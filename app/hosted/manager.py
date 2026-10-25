"""Per-client hosted browsers: screencast out, validated input in.

Each provisioned client gets ONE isolated Playwright persistent context
(profile dir per client, so portal logins survive restarts). The client never
sees a CDP endpoint: frames arrive through our WebSocket as JPEGs and input
leaves through a small whitelisted vocabulary of mouse/keyboard events that we
dispatch through Playwright (itself a controlled CDP Input.* forwarder).

Hard rules enforced here:

  * Navigation is restricted to the client's configured portal origins. Route
    interception aborts a disallowed document request before it hits the wire;
    a framenavigated watchdog catches anything that does not produce a request
    (about:, data:, javascript:) and steers back to the booking page.
  * No popups, no downloads, no file choosers — the remote surface is a page,
    not a desktop.
  * Input is accepted only while the client holds manual control; automation
    checks the same flag and pauses, so both sides never drive at once.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, async_playwright

from ..config import HostedConfig

logger = logging.getLogger(__name__)

# Keys the input forwarder accepts. Single characters are always fine; this
# set covers the named keys a client needs to complete a portal form.
ALLOWED_KEYS = {
    "Enter", "Tab", "Backspace", "Delete", "Escape", "Space",
    "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
    "Home", "End", "PageUp", "PageDown",
    "Shift", "Control", "Alt", "Meta", "CapsLock",
    *(f"F{i}" for i in range(1, 13)),
}

ALLOWED_BUTTONS = {"left", "middle", "right"}
MAX_TEXT_LEN = 2000
MAX_WHEEL = 2400
#: Frames a slow subscriber may buffer before we start dropping the oldest.
SUBSCRIBER_QUEUE = 90


def _origin(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    if parts.port and not (
        (parts.scheme == "https" and parts.port == 443)
        or (parts.scheme == "http" and parts.port == 80)
    ):
        return f"{parts.scheme}://{host}:{parts.port}"
    return f"{parts.scheme}://{host}"


def origin_allowed(url: str, allowed_origins: list[str]) -> bool:
    """Exact scheme+host+port match against the client's allowlist."""
    if url in ("about:blank", "about:blank#blocked"):
        return True
    if url.startswith(("chrome-error:", "data:", "javascript:", "file:", "blob:", "devtools:")):
        return False
    return _origin(url) in {o.rstrip("/").lower() for o in allowed_origins}


class InputRejected(RuntimeError):
    pass


class ClientBrowser:
    """One client's browser: context + page + screencast + control state."""

    def __init__(self, client: dict, cfg: HostedConfig):
        self.client = client
        self.client_id = int(client["id"])
        self.cfg = cfg
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._cdp = None
        self._pw = None  # manager-owned playwright instance
        self._screencast_running = False
        self._subscribers: set[asyncio.Queue] = set()
        self._manual = False
        self._last_activity = time.monotonic()
        self._seq = 0
        self._closed = False
        self.url = "about:blank"
        self.title = ""
        self.blocked: list[str] = []  # recent blocked navigations (diag feed)

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self, playwright) -> None:
        self._pw = playwright
        profile = self.cfg.browsers_dir / f"client-{self.client_id}"
        profile.mkdir(parents=True, exist_ok=True)
        self.context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=self.cfg.headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={
                "width": self.cfg.viewport_width,
                "height": self.cfg.viewport_height,
            },
            locale="en-GB",
            timezone_id="Asia/Karachi",
        )
        self.context.on("page", self._on_popup)
        await self.context.route("**/*", self._route_guard)
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self._attach(page)
        logger.info("hosted browser up for client %s (%s)", self.client_id, self.client["username"])

    async def _attach(self, page: Page) -> None:
        """Point screencast + watchdogs at a (possibly new) main page."""
        self.page = page
        page.on("framenavigated", self._on_navigated)
        page.on("close", self._on_page_closed)
        page.on("download", self._on_download)
        try:
            self._cdp = await self.context.new_cdp_session(page)
            self._cdp.on("Page.screencastFrame", self._on_frame)
            await self._cdp.send("Page.enable")
            # No native file dialog may ever surface to a remote client.
            await self._cdp.send("Page.setInterceptFileChooserDialog", {"enabled": True})
            await self._cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": self.cfg.screen_quality,
                    "maxWidth": self.cfg.screen_max_width,
                    "maxHeight": self.cfg.screen_max_height,
                    "everyNthFrames": 1,
                },
            )
            self._screencast_running = True
        except Exception:
            logger.exception("screencast attach failed for client %s", self.client_id)
        self.url = page.url
        try:
            self.title = await page.title()
        except Exception:
            self.title = ""

    async def ensure_page(self) -> Page:
        """The main page, recreating it if it was closed or crashed."""
        if self.page is None or self.page.is_closed():
            assert self.context is not None
            page = await self.context.new_page()
            await self._attach(page)
        return self.page

    async def stop(self) -> None:
        self._closed = True
        try:
            if self._cdp is not None and self._screencast_running:
                await self._cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            if self.context is not None:
                await self.context.close()
        except Exception:
            pass

    # ── navigation guard ─────────────────────────────────────────────────

    def _allowed(self, url: str) -> bool:
        origins = list(self.client["allowed_origins"])
        mock = self.cfg.mock_portal_url.rstrip("/")
        if not self.cfg.allow_mock_portal:
            # Even a stale/provisioned mock row cannot reach the mock portal
            # unless the operator explicitly enabled it for this run.
            origins = [o for o in origins if o.rstrip("/") != mock]
        return origin_allowed(url, origins)

    async def _route_guard(self, route) -> None:
        request = route.request
        if request.is_navigation_request() and not self._allowed(request.url):
            logger.warning(
                "blocked navigation for client %s -> %s", self.client_id, request.url
            )
            self._note_blocked(request.url)
            self.publish({"type": "nav_blocked", "url": request.url})
            await route.abort()
            asyncio.get_running_loop().call_later(
                0.4, lambda: asyncio.ensure_future(self._recover_nav())
            )
            return
        await route.continue_()

    def _on_navigated(self, frame) -> None:
        """Watchdog for navigations that produce no request (data:, javascript:)."""
        if self.page is None or frame != self.page.main_frame:
            return
        url = frame.url
        if url.startswith("chrome-error:"):
            return  # left behind by an aborted nav; recovery will steer back
        if not self._allowed(url):
            logger.warning("watchdog blocked client %s nav -> %s", self.client_id, url)
            self._note_blocked(url)
            self.publish({"type": "nav_blocked", "url": url})
            asyncio.get_running_loop().call_later(
                0.1, lambda: asyncio.ensure_future(self._recover_nav())
            )
            return
        self.url = url
        asyncio.ensure_future(self._push_status())

    async def _recover_nav(self) -> None:
        """Steer the tab back to the client's booking page after a blocked nav."""
        if self.page is None or self.page.is_closed():
            return
        try:
            if not self._allowed(self.page.url):
                await self.page.goto(
                    self.client["booking_url"], wait_until="domcontentloaded",
                    timeout=15_000,
                )
        except Exception:
            logger.debug("nav recovery failed", exc_info=True)

    def _note_blocked(self, url: str) -> None:
        self.blocked.append(url)
        del self.blocked[:-10]

    def _on_popup(self, page: Page) -> None:
        """No extra windows for remote clients; popup content never reaches them."""
        logger.info("closing popup for client %s", self.client_id)
        asyncio.ensure_future(page.close())

    def _on_page_closed(self, page: Page) -> None:
        if page is self.page:
            self._screencast_running = False

    def _on_download(self, _download) -> None:
        asyncio.ensure_future(_download.cancel())
        logger.info("cancelled download for client %s", self.client_id)

    # ── screencast ───────────────────────────────────────────────────────

    def _on_frame(self, params: dict) -> None:
        """CDP Page.screencastFrame: ack it (or the stream stalls) + publish."""
        session_id = params.get("sessionId")
        if session_id is not None and self._cdp is not None:
            asyncio.ensure_future(
                self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
            )
        self._seq += 1
        metadata = params.get("metadata") or {}
        self.publish(
            {
                "type": "frame",
                "data": params.get("data", ""),
                "seq": self._seq,
                "w": metadata.get("deviceWidth", self.cfg.viewport_width),
                "h": metadata.get("deviceHeight", self.cfg.viewport_height),
            }
        )

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        self._subscribers.add(queue)
        return queue

    async def capture_once(self) -> None:
        """Push one frame now, for a subscriber that just connected.

        The screencast only emits when the page repaints — a client joining an
        idle browser would otherwise stare at the 'connecting' overlay forever.
        """
        if self._cdp is None or self.page is None or self.page.is_closed():
            return
        try:
            shot = await self._cdp.send(
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": self.cfg.screen_quality},
            )
            data = shot.get("data")
            if data:
                self._seq += 1
                self.publish(
                    {
                        "type": "frame",
                        "data": data,
                        "seq": self._seq,
                        "w": self.cfg.viewport_width,
                        "h": self.cfg.viewport_height,
                    }
                )
        except Exception:
            logger.debug("one-shot capture failed", exc_info=True)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        """Fan an event out to this client's stream subscribers.

        Frames drop oldest-first under backpressure (the newest frame is the
        only one that matters); status/event messages are never dropped —
        they are tiny and the queue is deep enough.
        """
        for queue in list(self._subscribers):
            if event.get("type") == "frame":
                while queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def _push_status(self) -> None:
        if self.page is None or self.page.is_closed():
            return
        try:
            self.title = await self.page.title()
        except Exception:
            pass
        self.publish({"type": "status", **self.status()})

    def status(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "control": "manual" if self._manual else "automation",
            "blocked": list(self.blocked[-3:]),
        }

    # ── manual control ───────────────────────────────────────────────────

    @property
    def is_manual(self) -> bool:
        return self._manual

    def take_control(self) -> None:
        self._manual = True
        self._last_activity = time.monotonic()
        self.publish({"type": "status", **self.status()})

    def release_control(self) -> None:
        self._manual = False
        self.publish({"type": "status", **self.status()})

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    def _require_manual(self) -> None:
        if not self._manual:
            raise InputRejected("client does not hold manual control")
        self._last_activity = time.monotonic()

    # ── input forwarding (validated, whitelisted) ────────────────────────

    def _coords(self, x, y) -> tuple[int, int]:
        try:
            xi, yi = int(x), int(y)
        except (TypeError, ValueError):
            raise InputRejected("coordinates must be integers")
        if not (0 <= xi <= self.cfg.viewport_width and 0 <= yi <= self.cfg.viewport_height):
            raise InputRejected("coordinates outside the viewport")
        return xi, yi

    async def input_mouse(self, event: str, x, y, button: str = "left", clicks: int = 1) -> None:
        self._require_manual()
        xi, yi = self._coords(x, y)
        button = button if button in ALLOWED_BUTTONS else "left"
        page = await self.ensure_page()
        if event == "move":
            await page.mouse.move(xi, yi)
        elif event == "down":
            await page.mouse.down(button=button)
        elif event == "up":
            await page.mouse.up(button=button)
        elif event == "click":
            await page.mouse.click(xi, yi, button=button, click_count=max(1, min(int(clicks), 3)))
        else:
            raise InputRejected(f"unknown mouse event {event!r}")

    async def input_wheel(self, x, y, dx, dy) -> None:
        self._require_manual()
        xi, yi = self._coords(x, y)
        page = await self.ensure_page()
        await page.mouse.move(xi, yi)
        dxi = max(-MAX_WHEEL, min(MAX_WHEEL, int(dx or 0)))
        dyi = max(-MAX_WHEEL, min(MAX_WHEEL, int(dy or 0)))
        await page.mouse.wheel(dxi, dyi)

    async def input_key(self, event: str, key: str) -> None:
        self._require_manual()
        if not isinstance(key, str) or not key:
            raise InputRejected("key is required")
        if len(key) != 1 and key not in ALLOWED_KEYS:
            raise InputRejected(f"key {key!r} is not in the allowed set")
        name = "Space" if key in (" ", "Space") else key
        page = await self.ensure_page()
        if event == "press":
            await page.keyboard.press(name)
        elif event == "down":
            await page.keyboard.down(name)
        elif event == "up":
            await page.keyboard.up(name)
        else:
            raise InputRejected(f"unknown key event {event!r}")

    async def input_text(self, text: str) -> None:
        self._require_manual()
        if not isinstance(text, str) or not text:
            return
        page = await self.ensure_page()
        await page.keyboard.insert_text(text[:MAX_TEXT_LEN])


class BrowserHostManager:
    """Owns every ClientBrowser plus the shared Playwright instance."""

    def __init__(self, cfg: HostedConfig):
        self.cfg = cfg
        self._pw = None
        self._browsers: dict[int, ClientBrowser] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._reaper = asyncio.create_task(self._reap_idle(), name="hosted-idle-reaper")

    async def ensure_browser(self, client: dict) -> ClientBrowser:
        client_id = int(client["id"])
        existing = self._browsers.get(client_id)
        if existing is not None and existing.context is not None:
            return existing
        async with self._lock:
            existing = self._browsers.get(client_id)
            if existing is not None and existing.context is not None:
                return existing
            browser = ClientBrowser(client, self.cfg)
            await browser.start(self._pw)
            self._browsers[client_id] = browser
            return browser

    def browser_for(self, client_id: int) -> ClientBrowser | None:
        return self._browsers.get(client_id)

    async def stop_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        for browser in list(self._browsers.values()):
            try:
                await browser.stop()
            except Exception:
                logger.exception("failed to stop a hosted browser")
        self._browsers.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def _reap_idle(self) -> None:
        """Manual control is a lease, not a hand-off: idle clients lose it so a
        forgotten dashboard tab can never park automation forever."""
        while True:
            await asyncio.sleep(30)
            for browser in list(self._browsers.values()):
                if browser.is_manual and browser.idle_seconds() > self.cfg.manual_idle_seconds:
                    logger.info(
                        "client %s manual control idle for %.0fs — releasing",
                        browser.client_id, browser.idle_seconds(),
                    )
                    browser.release_control()

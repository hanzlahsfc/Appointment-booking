"""Per-client monitoring worker for the hosted build.

Mirrors CrawlerWorker's poll — navigate, verify session, scrape, diff, maybe
book — but drives the client's own hosted browser and follows the hosted rules:

  * while the client holds manual control, the worker never touches the page;
  * a bounced-to-login page is reported as `awaiting_login` instead of an
    error — in the hosted model the CLIENT logs in by hand through remote
    control, there are no stored portal credentials;
  * an unresolved or in-flight booking_log row parks polling exactly like the
    crawler (a slot may still be held), and that block survives restarts
    because it lives in SQLite;
  * assist-mode hand-off reaches the client as a stream event + dashboard
    status, not just the host's local alarm.

Booking guard rails all stay in BookingAgent/booking_log — this worker only
decides when it is safe to navigate.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..booking import BookingAgent, BookingStatus
from ..config import Config
from ..crawler import CrawlerWorker
from ..diff import select_bookable, sort_by_date
from ..site import BookingSelectors, PlaywrightSiteDriver, SiteNotConfigured
from ..store import Store
from ..util import iso_pkt, now_pkt
from .manager import BrowserHostManager

logger = logging.getLogger(__name__)

BLOCKED_RECHECK_SECONDS = 20


def register_client_profiles(cfg: Config, clients: list[dict]) -> None:
    """Append each client's profile to cfg.profiles.

    BookingAgent's per-profile opt-in guard (cfg.booking_enabled_for) only
    knows config profiles — registering clients there keeps the guard intact:
    booking still requires BOTH a master mode (assist) AND the client row's
    own auto_book flag. A client that exists only in hosted.db cannot book.
    """
    from ..config import ProfileConfig

    for client in clients:
        if cfg.profile_by_name(client["profile_name"]) is not None:
            continue
        cfg.profiles.append(
            ProfileConfig(
                name=client["profile_name"],
                email="",
                category=client["category"],
                location=client["location"],
                booking_url=client["booking_url"],
                auto_book=bool(client.get("auto_book")),
            )
        )


class ClientFeed:
    """The dashboard surface BookingAgent/CrawlerWorker push into, scoped to
    one client: every event also fans out to the client's live stream.

    Implements the same method names as DashboardState (set_status, set_slots,
    push_alert, set_booking, push_booking) so existing components work
    unchanged — but a ClientFeed can only ever carry its own client's data.
    """

    def __init__(self, browser_owner, profile_name: str):
        self._browser = browser_owner  # ClientBrowser — publishes to subscribers
        self.profile_name = profile_name
        self.state: dict = {
            "status": "starting",
            "message": "",
            "last_poll": None,
            "slots": [],
            "new_count": 0,
        }
        self.booking: dict = {}
        self.alerts: list[dict] = []
        self.bookings: list[dict] = []

    # DashboardState-compatible writers ───────────────────────────────────

    def set_status(self, profile: str, **fields) -> None:
        self.state.update(fields)
        self.state["ts"] = time.time()
        self._emit({"type": "monitor", "state": self._public_state()})

    def set_slots(self, profile: str, slots: list[dict]) -> None:
        self.state["slots"] = sorted(
            slots, key=lambda s: (s.get("date", ""), s.get("time") or "")
        )
        self._emit({"type": "monitor", "state": self._public_state()})

    def push_alert(self, slot: dict, alert_status: str) -> None:
        alert = {
            "ts": time.time(),
            "profile": slot.get("profile"),
            "category": slot.get("category"),
            "location": slot.get("location"),
            "date": slot.get("date"),
            "time": slot.get("time"),
            "status": alert_status,
        }
        self.alerts.insert(0, alert)
        del self.alerts[100:]
        self._emit({"type": "alert", "alert": alert})

    def set_booking(self, profile: str, **fields) -> None:
        self.booking.update(fields)
        self.booking["ts"] = time.time()
        self._emit({"type": "booking", "booking": dict(self.booking)})

    def push_booking(self, result: dict) -> None:
        entry = {
            "ts": time.time(),
            "profile": result.get("profile"),
            "status": result.get("status"),
            "stage": result.get("stage"),
            "date": (result.get("slot") or {}).get("date"),
            "time": (result.get("slot") or {}).get("time"),
            "confirmation_ref": result.get("confirmation_ref"),
            "error": result.get("error"),
        }
        self.bookings.insert(0, entry)
        del self.bookings[100:]
        self._emit({"type": "booking", "booking": entry})

    def notify_booking(self, result: dict) -> None:
        """Notifier-shaped sink: a booking result IS the client's alert."""
        self.push_booking(result)

    # ── helpers ──────────────────────────────────────────────────────────

    def _public_state(self) -> dict:
        return {
            "status": self.state.get("status"),
            "message": self.state.get("message", ""),
            "last_poll": self.state.get("last_poll"),
            "last_check": self.state.get("last_check"),
            "latency_ms": self.state.get("latency_ms"),
            "effective_interval_s": self.state.get("effective_interval_s"),
            "throttle": self.state.get("throttle"),
            "slots": self.state.get("slots", []),
            "new_count": self.state.get("new_count", 0),
        }

    def _emit(self, event: dict) -> None:
        self._browser.publish(event)

    def snapshot(self) -> dict:
        return {
            "monitor": self._public_state(),
            "booking": dict(self.booking),
            "alerts": list(self.alerts[:50]),
            "bookings": list(self.bookings[:50]),
        }


class HostedMonitor:
    """Poll loop bound to one client and their hosted browser."""

    def __init__(
        self,
        cfg: Config,
        client: dict,
        store: Store,
        manager: BrowserHostManager,
        alarm=None,
    ):
        self.cfg = cfg
        self.client = client
        self.profile_name = client["profile_name"]
        self.store = store
        self.manager = manager
        self._alarm = alarm
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._selectors: BookingSelectors | None = None
        self.feed: ClientFeed | None = None
        # single-flight accounting — one poll at a time, always
        self._in_flight = False
        self._last_poll_started: float | None = None
        self._last_check: float | None = None       # last successful portal read
        self._last_latency_ms: float | None = None
        self._effective_interval: float | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"hosted:{self.client['username']}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ── loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        browser = await self.manager.ensure_browser(self.client)
        self.feed = ClientFeed(browser, self.profile_name)
        selectors = BookingSelectors(
            **{
                k: v
                for k, v in (self.client.get("selectors") or {}).items()
                if k in BookingSelectors.__dataclass_fields__
            }
        )
        if isinstance(self.client.get("selectors", {}).get("token_inputs"), list):
            selectors.token_inputs = tuple(self.client["selectors"]["token_inputs"])
        self._selectors = selectors

        interval = max(5, self.cfg.hosted.poll_interval_seconds)
        while not self._stop.is_set():
            started = time.monotonic()
            if self._last_poll_started is not None:
                self._effective_interval = started - self._last_poll_started
            self._last_poll_started = started
            try:
                await self._poll_once(browser, selectors)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("hosted poll failed for %s", self.profile_name)
                self.feed.set_status(
                    self.profile_name, status="error",
                    message=f"{type(exc).__name__}: {exc}", last_poll=time.time(),
                )
            # Wait the configured interval — polls never overlap and never spin.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self, browser, selectors: BookingSelectors) -> None:
        """One check of the portal. Single-flight by construction: the loop
        awaits this and _in_flight guards any re-entry from elsewhere."""
        if self._in_flight:
            return
        self._in_flight = True
        try:
            await self._poll_body(browser, selectors)
        finally:
            self._in_flight = False

    async def _poll_body(self, browser, selectors: BookingSelectors) -> None:
        profile = self.profile_name

        # 1. A held reservation may have been finished by hand — check the
        #    current page (read-only) for the confirmation marker. This works
        #    even while the client holds manual control: reading is not
        #    control, and a submitted form already shows the marker.
        unresolved = self.store.unresolved_booking(profile)
        if unresolved and unresolved["status"] == "needs_human":
            ref = await self._check_completion(browser)
            if ref:
                self._record_completion(unresolved, ref)
            else:
                self._throttle_status(
                    "holding", "unresolved booking",
                    f"booking attempt #{unresolved['id']} is held — complete "
                    "the form in the browser, or resolve it",
                )
                return

        # 2. The client is driving — hands off entirely.
        if browser.is_manual:
            self._throttle_status(
                "manual_control", "manual control",
                "client is in control; automation paused",
            )
            return

        # 3. Any other block (in-flight attempt, aborted_unknown_state) parks
        #    polling. DB-backed — survives restarts.
        reason = self._block_reason()
        if reason:
            self._throttle_status("holding", "unresolved booking", reason)
            return

        page = await browser.ensure_page()
        driver = PlaywrightSiteDriver(page, selectors)

        t0 = time.perf_counter()
        await driver.open_booking_page(self.client["booking_url"])
        await driver.wait_for_waf()

        if await driver.is_login_page():
            # No stored credentials exist for hosted clients — the human on the
            # other end of the screencast does the portal sign-in.
            self._throttle_status(
                "awaiting_login", "awaiting login",
                "portal sign-in required — take control and log in",
            )
            return

        if not selectors.configured or not selectors.slot_row:
            self._throttle_status(
                "error", "configuration",
                "monitoring selectors are not configured for this client",
            )
            return

        await driver.apply_filters(self.client["location"], self.client["category"])
        rows = await driver.list_slots()
        slots = self._normalize(rows)
        dur = time.perf_counter() - t0
        self._last_check = time.time()
        self._last_latency_ms = round(dur * 1000)

        new_slots = self._process(slots)
        self.feed.set_status(
            profile, status="ok", message="", last_poll=time.time(),
            last_check=self._last_check, latency_ms=self._last_latency_ms,
            effective_interval_s=self._effective_interval, throttle=None,
        )
        self.feed.set_slots(profile, slots)
        logger.info(
            "HOSTED OK | client=%s | slots=%d | new=%d | dur=%.1fs",
            self.client["username"], len(slots), len(new_slots), dur,
        )

        if new_slots and not browser.is_manual:
            await self._maybe_book(browser, driver, new_slots)

    def _throttle_status(self, status: str, throttle: str, message: str) -> None:
        self.feed.set_status(
            self.profile_name, status=status, message=message,
            last_poll=time.time(), last_check=self._last_check,
            latency_ms=self._last_latency_ms,
            effective_interval_s=self._effective_interval, throttle=throttle,
        )

    # ── held-booking completion ──────────────────────────────────────────

    async def _check_completion(self, browser) -> str | None:
        """Read-only look at the current page for the confirmation marker."""
        selectors = self._selectors
        if not selectors or not selectors.confirmation_marker:
            return None
        page = browser.page
        if page is None or page.is_closed():
            return None
        try:
            return await PlaywrightSiteDriver(page, selectors).confirmation_ref()
        except Exception:
            return None

    def _record_completion(self, row: dict, confirmation_ref: str) -> None:
        finished = iso_pkt(now_pkt())
        if not self.store.complete_booking(row["id"], confirmation_ref, finished):
            return
        slot = {
            "profile": self.profile_name,
            "location": self.client["location"],
            "category": self.client["category"],
            "date": row["date"],
            "time": row.get("time"),
        }
        self.store.mark_booked(slot)
        logger.warning(
            "BOOKED | profile=%s | attempt=#%s | ref=%s — completed manually "
            "by the client; no further bookings for this profile",
            self.profile_name, row["id"], confirmation_ref,
        )
        self.feed.set_booking(
            self.profile_name, status="booked", stage="manual",
            slot=slot, confirmation_ref=confirmation_ref, finished_at=finished,
        )
        self.feed.set_status(
            self.profile_name, status="booked",
            message=f"booking confirmed ({confirmation_ref}); this profile "
                    "cannot book again",
            last_check=self._last_check, latency_ms=self._last_latency_ms,
            effective_interval_s=self._effective_interval, throttle=None,
        )

    async def check_completion_now(self) -> dict:
        """On-demand completion check for the dashboard's 'I finished' button."""
        unresolved = self.store.unresolved_booking(self.profile_name)
        if not unresolved:
            return {"pending": False, "booked": self.store.has_booked(self.profile_name)}
        if unresolved["status"] != "needs_human":
            return {"pending": True, "status": unresolved["status"]}
        browser = self.manager.browser_for(self.client["id"])
        if browser is None:
            return {"pending": True, "status": "needs_human",
                    "error": "browser is not running"}
        ref = await self._check_completion(browser)
        if ref:
            self._record_completion(unresolved, ref)
            return {"pending": False, "booked": True,
                    "confirmation_ref": ref}
        return {"pending": True, "status": "needs_human",
                "error": "no confirmation marker on the page yet"}

    # ── slot processing + booking ────────────────────────────────────────

    def _normalize(self, rows: list[str]) -> list[dict]:
        """Same label formats as the crawler (YYYY-MM-DD, DD.MM.YYYY, ...)."""
        out = []
        for raw in rows:
            parsed = CrawlerWorker._parse_slot_label(raw)
            if parsed is None:
                continue
            date_str, time_str = parsed
            out.append(
                {
                    "profile": self.profile_name,
                    "location": self.client["location"],
                    "category": self.client["category"],
                    "date": date_str,
                    "time": time_str,
                    "raw": raw,
                }
            )
        return out

    def _process(self, slots: list[dict]) -> list[dict]:
        now = iso_pkt(now_pkt())
        new_slots = self.store.upsert_slots(slots, seen_at=now)
        scope = {(s["date"], s["time"] or "") for s in slots}
        self.store.deactivate_missing(
            self.profile_name, self.client["location"], self.client["category"],
            scope, seen_at=now,
        )
        new_slots = sort_by_date(new_slots)
        for slot in new_slots:
            slot["email"] = ""
            slot["booking_url"] = self.client["booking_url"]
            # The dashboard event is the alert; recording it in alert_log keeps
            # the books consistent and marks the slot so it never re-alerts.
            self.store.mark_alert_attempted([slot])
            self.store.mark_alerted([slot])
            self.store.log_alert(
                slot, sent_at=now,
                subject=f"NEW SLOT: {slot['category']} @ {slot['location']} — {slot['date']}",
                status="dashboard",
            )
            if self.feed:
                self.feed.push_alert(slot, "dashboard")
        return new_slots

    async def _maybe_book(self, browser, driver, new_slots: list[dict]) -> None:
        if not self.cfg.booking_enabled_for(self.profile_name):
            return
        slot = select_bookable(new_slots, {})
        if slot is None:
            return
        # Re-check immediately before acting: a client who grabbed control
        # during this poll must not find the agent clicking under them.
        if browser.is_manual:
            return
        agent = BookingAgent(
            self.cfg,
            self._profile_dict(),
            self.store,
            alarm=self._alarm,
            dashboard=self.feed,
            notifier=self.feed,
        )
        result = await agent.book(slot, driver)
        if result.status == BookingStatus.NEEDS_HUMAN.value:
            # The remote hand-off: the client sees this on their stream and in
            # the dashboard, then takes control and finishes the form. Polling
            # stays parked via the unresolved booking row until resolved.
            self.feed.set_status(
                self.profile_name, status="needs_human",
                message="a slot was claimed — take control and finish the form",
            )

    def _profile_dict(self) -> dict:
        return {
            "name": self.profile_name,
            "email": "",
            "category": self.client["category"],
            "location": self.client["location"],
            "booking_url": self.client["booking_url"],
            "auto_book": self.client.get("auto_book", False),
            "prefer": {},
        }

    def _block_reason(self) -> str | None:
        open_row = self.store.open_booking(self.profile_name)
        if open_row:
            return (
                f"booking attempt #{open_row['id']} never finished — a slot may "
                "still be held; resolve it before polling resumes"
            )
        unresolved = self.store.unresolved_booking(self.profile_name)
        if unresolved:
            return (
                f"booking attempt #{unresolved['id']} ended {unresolved['status']} "
                "— a slot may still be held; resolve it before polling resumes"
            )
        return None

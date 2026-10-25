"""Playwright context factory + Incapsula challenge handling."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

INCAPSULA_MARKERS = ("_Incapsula_Resource", "distil_", "/_Incapsula_")
CHALLENGE_WAIT_SECONDS = 60
CHALLENGE_POLL = 2


class WAFError(Exception):
    """Raised when Imperva/Incapsula blocks or challenges the client."""
    pass


class WAFBlocked(WAFError):
    """Hard IP/range block (Incapsula 'Error 17 — Access denied')."""


class IncapsulaHandler:
    """Waits for the Incapsula JS challenge to resolve on a page."""

    def __init__(self, markers: tuple[str, ...] = INCAPSULA_MARKERS, max_wait: int = CHALLENGE_WAIT_SECONDS):
        self.markers = markers
        self.max_wait = max_wait

    async def wait_for_clear(self, page: Page, expect_text: str | None = None) -> None:
        """Block until the challenge is cleared.

        Pauses while the URL/keyword match `expect_text` is absent. If the WAF
        shell is still present after max_wait, raise WAFError. A hard IP block
        ('Access denied', 'Error 17', 'incident_id') is detected immediately.
        """
        elapsed = 0.0
        while True:
            url = page.url
            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=2000)
            except Exception:
                pass
            for f in page.frames:
                try:
                    t = (await f.locator("body").inner_text(timeout=800)).strip()
                    body_text += "\n" + t
                except Exception:
                    continue
            # hard IP/range rejections show immediately — surface them
            low = body_text.lower()
            if "access denied" in low or "error 17" in low:
                raise WAFBlocked(
                    "Imperva blocked this client (Access denied / Error 17). "
                    "The egress IP is flagged — use a different network or a proxy."
                )
            if not any(m in url for m in self.markers):
                if expect_text is None or expect_text.lower() in body_text.lower():
                    return
            if elapsed >= self.max_wait:
                raise WAFError(f"Incapsula challenge did not clear after {self.max_wait}s")
            await asyncio.sleep(CHALLENGE_POLL)
            elapsed += CHALLENGE_POLL


async def create_browser(headless: bool = True) -> Browser:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    return browser


async def new_context(
    browser: Browser,
    storage_state: str | Path | None = None,
    locale: str = "en-GB",
    proxy: str | None = None,
) -> BrowserContext:
    kwargs = {
        "locale": locale,
        "viewport": {"width": 1366, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "timezone_id": "Asia/Karachi",
    }
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    if storage_state:
        state_path = Path(storage_state)
        if state_path.exists():
            kwargs["storage_state"] = str(state_path)
    context = await browser.new_context(**kwargs)
    return context


async def get_page(context: BrowserContext) -> Page:
    if context.pages:
        return context.pages[0]
    return await context.new_page()


@dataclass
class BrowserSession:
    """A browser we own end to end, plus the Playwright instance behind it."""

    context: BrowserContext
    playwright: Any

    async def aclose(self) -> None:
        try:
            await self.context.close()
        finally:
            try:
                await self.playwright.stop()
            except Exception:
                pass


async def launch_persistent(
    profile_dir: str | Path,
    headless: bool = True,
    proxy: str | None = None,
    locale: str = "en-GB",
) -> BrowserSession:
    """Launch Chrome on a persistent profile directory.

    This is what keeps the session warm. `storage_state` only carries cookies and
    localStorage; a real profile also keeps IndexedDB, service workers and the
    cache, so the WAF and the site's own session behave as they do for a human.
    The human signs in once (reCAPTCHA included) and later runs reuse it.
    """
    path = Path(profile_dir)
    path.mkdir(parents=True, exist_ok=True)
    playwright = await async_playwright().start()

    kwargs: dict[str, Any] = {
        "user_data_dir": str(path),
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
        "locale": locale,
        "viewport": {"width": 1366, "height": 900},
        "timezone_id": "Asia/Karachi",
    }
    if proxy:
        kwargs["proxy"] = {"server": proxy}

    try:
        context = await playwright.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception as exc:
        logger.warning("system Chrome unavailable (%s); using bundled Chromium", exc)
        context = await playwright.chromium.launch_persistent_context(**kwargs)
    logger.info("persistent browser profile: %s (headless=%s)", path, headless)
    return BrowserSession(context=context, playwright=playwright)
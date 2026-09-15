"""Playwright context factory + Incapsula challenge handling."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

INCAPSULA_MARKERS = ("_Incapsula_Resource", "distil_", "/_Incapsula_")
CHALLENGE_WAIT_SECONDS = 60
CHALLENGE_POLL = 2


class WAFError(Exception):
    pass


class IncapsulaHandler:
    """Waits for the Incapsula JS challenge to resolve on a page."""

    def __init__(self, markers: tuple[str, ...] = INCAPSULA_MARKERS, max_wait: int = CHALLENGE_WAIT_SECONDS):
        self.markers = markers
        self.max_wait = max_wait

    async def wait_for_clear(self, page: Page, expect_text: str | None = None) -> None:
        """Block until the challenge is cleared.

        Pauses while the URL/keyword match `expect_text` is absent. If the WAF
        shell is still present after max_wait, raise WAFError.
        """
        elapsed = 0.0
        while True:
            url = page.url
            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=2000)
            except Exception:
                pass
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


async def new_context(browser: Browser, storage_state: str | Path | None = None, locale: str = "en-GB") -> BrowserContext:
    kwargs = {
        "locale": locale,
        "viewport": {"width": 1366, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "timezone_id": "Asia/Karachi",
    }
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
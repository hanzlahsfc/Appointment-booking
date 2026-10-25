"""Everything that depends on the real GVCW DOM.

All site-specific knowledge — selectors, form field names, token inputs — is
gathered into BookingSelectors so the recon pass becomes a config change rather
than a code change. Fill it from recon/booking-flow.json (see recon.py).

Until those selectors are filled, the driver refuses to act rather than
clicking blindly: guessing on a visa portal can book the wrong appointment.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from playwright.async_api import Page

from .browser import IncapsulaHandler
from .session import LOGIN_URL

logger = logging.getLogger(__name__)


class SiteNotConfigured(RuntimeError):
    """Selectors have not been filled in from the recon pass yet."""


class SlotNotFound(RuntimeError):
    """The slot we meant to claim is no longer offered."""


@dataclass
class BookingSelectors:
    login_url: str = LOGIN_URL
    vac_select: str | None = None
    category_select: str | None = None
    slot_row: str | None = None
    reserve_button: str | None = None
    applicant_fields: dict[str, str] = field(default_factory=dict)
    submit_button: str | None = None
    confirmation_marker: str | None = None
    token_inputs: tuple[str, ...] = ("form_build_id", "form_token", "form_id")
    # Filled after recon; the hybrid reserve-POST path uses these once we know
    # the endpoint. Unused until then — see "deferred" note in the docs.
    reserve_endpoint: str | None = None
    csrf_header: str | None = None

    @property
    def missing(self) -> list[str]:
        required = {
            "vac_select": self.vac_select,
            "category_select": self.category_select,
            "slot_row": self.slot_row,
            "reserve_button": self.reserve_button,
        }
        return [name for name, value in required.items() if not value]

    @property
    def configured(self) -> bool:
        return not self.missing


def load_selectors(path: str | Path) -> BookingSelectors:
    """Load selectors from JSON, falling back to the built-in defaults."""
    file = Path(path)
    if not file.exists():
        return BookingSelectors()
    try:
        raw: dict[str, Any] = json.loads(file.read_text())
    except (ValueError, OSError) as exc:
        logger.warning("could not read selectors file %s: %s", file, exc)
        return BookingSelectors()
    known = {f for f in asdict(BookingSelectors())}
    clean = {k: v for k, v in raw.items() if k in known}
    if "token_inputs" in clean and isinstance(clean["token_inputs"], list):
        clean["token_inputs"] = tuple(clean["token_inputs"])
    return BookingSelectors(**clean)


class SiteDriver(Protocol):
    """The surface BookingAgent drives. Faking this keeps the agent testable."""

    async def open_booking_page(self, url: str) -> None: ...
    async def wait_for_waf(self) -> None: ...
    async def locate(self, slot: dict) -> bool: ...
    async def reserve(self, slot: dict) -> None: ...
    async def read_tokens(self) -> dict[str, str]: ...
    async def fill_applicant(self, data: dict) -> None: ...
    async def submit(self) -> None: ...
    async def confirmation_ref(self) -> str | None: ...
    async def screenshot(self, path: Path) -> None: ...
    async def content(self) -> str: ...


class PlaywrightSiteDriver:
    """Playwright implementation of the site interaction."""

    def __init__(self, page: Page, selectors: BookingSelectors):
        self.page = page
        self.selectors = selectors

    def _require_configured(self) -> None:
        if not self.selectors.configured:
            raise SiteNotConfigured(
                "booking selectors are not configured (missing: "
                + ", ".join(self.selectors.missing)
                + "). Run recon.py and fill recon/booking-selectors.json."
            )

    async def open_booking_page(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    async def wait_for_waf(self) -> None:
        await IncapsulaHandler().wait_for_clear(self.page)

    async def _select(self, selector: str, value: str) -> None:
        """Drupal select boxes: try by value, then by visible label text."""
        location = self.page.locator(selector).first
        try:
            await location.select_option(value=value, timeout=8_000)
            return
        except Exception:
            pass
        await location.select_option(label=value, timeout=8_000)

    async def locate(self, slot: dict) -> bool:
        self._require_configured()
        if self.selectors.vac_select and slot.get("location"):
            await self._select(self.selectors.vac_select, str(slot["location"]))
        if self.selectors.category_select and slot.get("category"):
            await self._select(self.selectors.category_select, str(slot["category"]))
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        return await self._find_row(slot) is not None

    async def _find_row(self, slot: dict):
        """The row matching this slot's date (and time when known)."""
        rows = self.page.locator(self.selectors.slot_row or "")
        try:
            count = await rows.count()
        except Exception:
            return None
        wanted_date = str(slot.get("date") or "")
        wanted_time = str(slot.get("time") or "")
        for i in range(count):
            row = rows.nth(i)
            try:
                text = (await row.inner_text()).strip()
            except Exception:
                continue
            if wanted_date and wanted_date not in text:
                continue
            if wanted_time and wanted_time not in text:
                continue
            return row
        return None

    async def reserve(self, slot: dict) -> None:
        self._require_configured()
        row = await self._find_row(slot)
        if row is None:
            raise SlotNotFound(f"{slot.get('date')} {slot.get('time') or ''} is no longer listed")
        button = self.selectors.reserve_button or ""
        scoped = row.locator(button).first
        try:
            if await scoped.count() == 0:
                scoped = self.page.locator(button).first
        except Exception:
            scoped = self.page.locator(button).first
        await scoped.click(timeout=15_000)

    async def read_tokens(self) -> dict[str, str]:
        """Names present, values only for diagnostics — never logged."""
        tokens: dict[str, str] = {}
        for name in self.selectors.token_inputs:
            try:
                element = self.page.locator(f"input[name='{name}']").first
                if await element.count():
                    tokens[name] = await element.get_attribute("value") or ""
            except Exception:
                continue
        return tokens

    async def fill_applicant(self, data: dict) -> None:
        if not self.selectors.applicant_fields:
            raise SiteNotConfigured("applicant_fields are not configured; cannot fill the form")
        for field_name, selector in self.selectors.applicant_fields.items():
            if field_name not in data:
                continue
            value = data[field_name]
            if value is None or value == "":
                continue
            await self.page.locator(selector).first.fill(str(value), timeout=15_000)

    async def submit(self) -> None:
        if not self.selectors.submit_button:
            raise SiteNotConfigured("submit_button is not configured")
        await self.page.locator(self.selectors.submit_button).first.click(timeout=15_000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass

    async def confirmation_ref(self) -> str | None:
        marker = self.selectors.confirmation_marker
        if not marker:
            raise SiteNotConfigured("confirmation_marker is not configured")
        try:
            locator = self.page.locator(marker).first
            if await locator.count():
                return (await locator.inner_text()).strip() or None
        except Exception:
            return None
        return None

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=True)

    async def content(self) -> str:
        return await self.page.content()

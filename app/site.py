"""Everything that depends on the real GVCW DOM.

All site-specific knowledge — selectors, form field names, token inputs — is
gathered into BookingSelectors so the recon pass becomes a config change rather
than a code change. Fill it from recon/booking-flow.json (see recon.py).

Until those selectors are filled, the driver refuses to act rather than
clicking blindly: guessing on a visa portal can book the wrong appointment.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from playwright.async_api import Page

from .bridge import BridgeClient, BridgeError, BridgeNotConnected
from .browser import IncapsulaHandler, WAFBlocked
from .session import LOGIN_URL

logger = logging.getLogger(__name__)

WAF_BLOCK_MARKERS = ("access denied", "error 17", "incapsula incident")


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


def require_configured(selectors: BookingSelectors) -> None:
    """Refuse to act while the selectors are unknown — never guess on a portal."""
    if not selectors.configured:
        raise SiteNotConfigured(
            "booking selectors are not configured (missing: "
            + ", ".join(selectors.missing)
            + "). Run recon.py and fill recon/booking-selectors.json."
        )


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
        require_configured(self.selectors)

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


def _js_string(value: Any) -> str:
    """A safe JS string literal (never build selectors by concatenation)."""
    return json.dumps("" if value is None else str(value))


class BridgeSiteDriver:
    """Drives the tab the operator picked, through our browser extension.

    This is the professionally interesting one: it operates the browser the
    consultant is *already signed into*, so the Imperva challenge is long
    cleared and no reCAPTCHA login is needed. Clicks are dispatched as real
    mouse events at the element's centre rather than JS `.click()`, because a
    form may only react to trusted events. The extension shows a
    "debugging this browser" banner while attached, which is honest and expected.
    """

    def __init__(
        self,
        client: BridgeClient,
        selectors: BookingSelectors,
        tab_id: int | None = None,
    ):
        self.client = client
        self.selectors = selectors
        self.tab_id = tab_id if tab_id is not None else client.tab_id

    # ── primitives ───────────────────────────────────────────────────────

    async def _eval(self, expression: str, timeout: float = 30.0) -> Any:
        return await self.client.evaluate(expression, self.tab_id, timeout)

    async def _click_at(self, rect: dict) -> None:
        x, y = rect["x"], rect["y"]
        for params in (
            {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        ):
            await self.client.command("Input.dispatchMouseEvent", params, self.tab_id)

    @staticmethod
    def _rect_js(element_expression: str) -> str:
        return (
            "(() => { const el = %s; if (!el) return null;"
            " el.scrollIntoView({block:'center'});"
            " const r = el.getBoundingClientRect();"
            " if (!r.width && !r.height) return null;"
            " return { x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) }; })()"
            % element_expression
        )

    def _row_js(self, slot: dict, body: str) -> str:
        """Bind `row` to the matching slot row, then run `body`."""
        return (
            "(() => { const rows = [...document.querySelectorAll(%s)];"
            " const d = %s, t = %s;"
            " const row = rows.find(r => r.innerText.includes(d)"
            " && (!t || r.innerText.includes(t)));"
            " if (!row) return null; %s })()"
            % (
                _js_string(self.selectors.slot_row or ""),
                _js_string(slot.get("date") or ""),
                _js_string(slot.get("time") or ""),
                body,
            )
        )

    async def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await self._eval("document.readyState", timeout=10) == "complete":
                    return
            except BridgeError:
                pass
            await asyncio.sleep(0.5)

    async def _set_field(self, selector: str, value: Any) -> bool:
        js = (
            "(() => { const el = document.querySelector(%s); if (!el) return false;"
            " el.focus(); el.value = %s;"
            " el.dispatchEvent(new Event('input', {bubbles:true}));"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
            " el.dispatchEvent(new Event('blur', {bubbles:true}));"
            " return true; })()" % (_js_string(selector), _js_string(value))
        )
        return bool(await self._eval(js))

    # ── SiteDriver ───────────────────────────────────────────────────────

    async def open_booking_page(self, url: str) -> None:
        await self.client.command("Page.navigate", {"url": url}, self.tab_id)
        await self._wait_ready()

    async def wait_for_waf(self) -> None:
        """The operator's own browser normally passes straight through."""
        text = await self._eval("document.body ? document.body.innerText : ''")
        low = (text or "").lower()
        for marker in WAF_BLOCK_MARKERS:
            if marker in low:
                raise WAFBlocked(
                    f"the portal returned the block shell ({marker}) for this tab"
                )

    async def locate(self, slot: dict) -> bool:
        require_configured(self.selectors)
        if self.selectors.vac_select:
            await self._set_field(self.selectors.vac_select, slot.get("location"))
        if self.selectors.category_select:
            await self._set_field(self.selectors.category_select, slot.get("category"))
        await self._wait_ready(timeout=15)
        found = await self._eval(self._row_js(slot, "return true;"))
        return bool(found)

    async def reserve(self, slot: dict) -> None:
        require_configured(self.selectors)
        button = self.selectors.reserve_button or ""
        rect_js = self._row_js(
            slot,
            self._rect_js(f"row.querySelector({_js_string(button)})"),
        )
        rect = await self._eval(rect_js)
        if not rect:
            raise SlotNotFound(
                f"{slot.get('date')} {slot.get('time') or ''} is no longer offered"
            )
        await self._click_at(rect)

    async def read_tokens(self) -> dict[str, str]:
        tokens: dict[str, str] = {}
        for name in self.selectors.token_inputs:
            value = await self._eval(
                "(() => { const el = document.querySelector(%s);"
                " return el ? el.value : null; })()" % _js_string(f"input[name='{name}']")
            )
            if value is not None:
                tokens[name] = value
        return tokens

    async def fill_applicant(self, data: dict) -> None:
        if not self.selectors.applicant_fields:
            raise SiteNotConfigured("applicant_fields are not configured; cannot fill the form")
        for field_name, selector in self.selectors.applicant_fields.items():
            value = data.get(field_name)
            if value is None or value == "":
                continue
            if not await self._set_field(selector, value):
                raise SiteNotConfigured(f"applicant field not found on the page: {field_name}")

    async def submit(self) -> None:
        if not self.selectors.submit_button:
            raise SiteNotConfigured("submit_button is not configured")
        rect = await self._eval(self._rect_js(f"document.querySelector({_js_string(self.selectors.submit_button)})"))
        if not rect:
            raise SiteNotConfigured("submit button not found on the page")
        await self._click_at(rect)
        await self._wait_ready(timeout=60)

    async def confirmation_ref(self) -> str | None:
        if not self.selectors.confirmation_marker:
            raise SiteNotConfigured("confirmation_marker is not configured")
        return await self._eval(
            "(() => { const el = document.querySelector(%s);"
            " return el ? el.innerText.trim() : null; })()"
            % _js_string(self.selectors.confirmation_marker)
        )

    async def screenshot(self, path: Path) -> None:
        data = await self.client.screenshot_b64(self.tab_id)
        if not data:
            return
        import base64

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))

    async def content(self) -> str:
        return await self._eval("document.documentElement.outerHTML") or ""

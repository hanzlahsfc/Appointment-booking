"""GVCW portal driver — the search-driven picker decoded from /dist/js/app.js.

The real pk-gr-services portal does not expose a select-and-list picker like
the mock: availability lives behind a Search action that issues
PUT /api/v1/periodslot/slots and renders a grid of divs:

  #resultDiv > table#result > td > div#t_{periodslotid}
      .appointment_slot_enabled     — bookable (has onclick=selectTime)
      .appointment_slot_notselectable — visible but unpickable
      .appointment_slot_disabled    — full
  empty day: #resultMessage loses .hidden, #result is absent

Slot *selection* is client-side only (selectTime sets #selectedtime +
#gp_periodslotid). The actual reservation is #btn-new-appointment behind an
SMS OTP (#btn-onetimepassword → #onetimepassword) and reCAPTCHA, so the flow
is: search → select → prefill → human requests OTP / solves captcha →
explicit confirm → verify /appointments/result/{ref}.

A failed or unreadable search NEVER reports zero slots — timeouts and
missing markers raise SiteNotConfigured instead.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page, TimeoutError as PwTimeout

from .site import BookingSelectors, SiteNotConfigured, SlotNotFound

logger = logging.getLogger(__name__)

# ── decoded real-portal selectors (recon/booking-selectors.json) ─────────
SEL_TYPE = "#type"
SEL_BOOKINGFOR = "#bookingfor"
SEL_MEMBERS = "#members"
SEL_METHOD = "#appointmentmethod"
SEL_DATEFROM = "#datefrom"
SEL_SEARCH = "#btn-search"
SEL_RESULT = "#result"
SEL_RESULT_MSG = "#resultMessage"
SEL_RESULT_DIV = "#resultDiv"
SEL_SLOT = ".appointment_slot"
SEL_SLOT_ENABLED = ".appointment_slot_enabled"
SEL_SLOT_SELECTED = "appointment_slot_selected"
SEL_SELECTEDTIME = "#selectedtime"
SEL_GP_PERIOD = "#gp_periodslotid"
SEL_SUBMIT_INFO = "#submitinfo"
SEL_OTP_BTN = "#btn-onetimepassword"
SEL_OTP_INPUT = "#onetimepassword"
SEL_OTP_WRAP = "#onetimepassword-wrap"
SEL_BOOK_BTN = "#btn-new-appointment"
SEL_ALERT_ERR = ".alertify .ajs-message.ajs-error, .alertify .ajs-error"
SEL_NO_RESULTS = "#no-results"

CONFIRM_URL = "/appointments/result/"

APPLICANT_FIELD_MAP = {
    "surname": "#gp_surname",
    "firstname": "#gp_firstname",
    "dateofbirth": "#gp_dateofbirth",
    "passportnumber": "#gp_passportnumber",
    "traveldocumentvaliduntil": "#gp_traveldocumentvaliduntil",
    "email": "#ind_email",
    "phonenumber": "#ind_phonenumber",
}
APPLICANT_SELECT_MAP = {
    "gender": "#gp_gender",
    "nationality": "#gp_nationality",
    "phone_prefix": "#phonenumberprefix",
}

DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _iso_to_dmy(iso: str) -> str:
    """2026-11-30 -> 30/11/2026"""
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def _dmy_to_iso(dmy: str) -> str | None:
    m = DATE_RE.search(dmy or "")
    if not m:
        return None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


class GvcwSiteDriver:
    """SiteDriver for the real GVCW booking page (/appointments/add)."""

    def __init__(self, page: Page, client: dict | None = None):
        self.page = page
        self.client = client or {}
        # BookingAgent's _validate_auto reads driver.selectors
        self.selectors = BookingSelectors(
            submit_button=SEL_BOOK_BTN,
            confirmation_marker=CONFIRM_URL,
            applicant_fields=APPLICANT_FIELD_MAP,
        )
        self._last_slots: dict[str, int] = {}  # "DD/MM/YYYY HH:MM" -> periodslotid

    # ── navigation ───────────────────────────────────────────────────────

    async def open_booking_page(self, url: str) -> None:
        if self.page.url != url and CONFIRM_URL not in self.page.url:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    async def wait_for_waf(self) -> None:
        if await self.is_login_page():
            return
        # Incapsula challenge pages auto-refresh; give the page a moment, then
        # wait until either the form or a challenge frame is present.
        try:
            await self.page.wait_for_selector(
                f"{SEL_TYPE}, {SEL_DATEFROM}", timeout=15_000
            )
        except PwTimeout:
            raise SiteNotConfigured(
                "booking form did not appear — challenge page or block? "
                f"(url={self.page.url!r})"
            )

    async def is_login_page(self) -> bool:
        """A lapsed session shows the login form at ANY url (the real portal
        can also answer /appointments/add with a bare 401 page), so check the
        DOM (#username/#password/#btn-login), not just the URL."""
        if "/login" in self.page.url:
            return True
        try:
            return bool(
                await self.page.evaluate(
                    "() => (document.querySelector('#username') &&"
                    "       document.querySelector('#password')) ||"
                    "      document.title.startsWith('401') ||"
                    "      (document.body && document.body.innerText.includes("
                    "          'not verify that you are authorized'))"
                )
            )
        except Exception:
            return False

    # ── search / detection ───────────────────────────────────────────────

    async def _jq_set(self, selector: str, value: str) -> None:
        """select2/datepickers only react to jQuery change events."""
        await self.page.evaluate(
            "([sel, v]) => { const el = document.querySelector(sel); if (!el) return false;"
            " el.value = v; el.dispatchEvent(new Event('change', {bubbles:true}));"
            " if (window.jQuery) jQuery(sel).val(v).trigger('change'); return true; }",
            [selector, value],
        )

    async def apply_filters(self, location: str | None, category: str | None) -> None:
        """Set the search fields. `category` is the #type option value (e.g.
        '26' seasonal / '2' national D). Location is a no-op — the VAC is
        bound to the account server-side."""
        if not category:
            raise SiteNotConfigured("gvcw driver needs a #type value as category")
        await self._jq_set(SEL_TYPE, category)
        bookingfor = str(self.client.get("bookingfor", "0"))
        await self._jq_set(SEL_BOOKINGFOR, bookingfor)
        # setPeriodslot() refuses to run while #members is empty. For
        # Individual the select only carries group sizes (2..N) — the value
        # '1' has no option, so it must be injected before it can be set.
        members = str(self.client.get("members", "1"))
        await self.page.evaluate(
            "([sel, v]) => { const el = document.querySelector(sel);"
            " if (!el) return false;"
            " if (![...el.options].some(o => o.value === v)) {"
            "   const o = document.createElement('option');"
            "   o.value = v; o.text = v; el.add(o); }"
            " el.value = v; el.dispatchEvent(new Event('change',{bubbles:true}));"
            " if (window.jQuery) jQuery(sel).val(v).trigger('change');"
            " return el.value === v; }",
            [SEL_MEMBERS, members],
        )
        if bookingfor == "0":
            await self._jq_set(SEL_METHOD, "1")
        else:
            await self._jq_set(SEL_METHOD, str(self.client.get("method", "1")))

    async def _search(self, date_dmy: str) -> None:
        """Fill #datefrom and fire Search; wait until the portal shows either
        the result grid or the empty-state marker. Anything else is a failed
        read — never silently zero.

        Freshness: an old #result/#resultMessage from a previous date must
        not satisfy a NEW search. We snapshot the marker state before the
        click, and require a fresh signal — either a new grid, or the empty
        marker appearing after having been hidden."""
        await self._jq_set(SEL_DATEFROM, date_dmy)
        # Baseline: what the page shows BEFORE this search fires.
        before = await self.page.evaluate(
            "() => { const m = document.querySelector('#resultMessage');"
            " const g = document.querySelector('#result');"
            " return { hasGrid: !!g, gridText: g ? g.innerText : '',"
            "          msgShown: !!(m && !m.classList.contains('hidden')),"
            "          msgText: m ? m.innerText : '' }; }"
        ) or {}
        await self.page.click(SEL_SEARCH)
        # Fresh = a new grid appeared (or changed), or the empty marker
        # (re)appeared/changed — leftovers from an earlier date never count.
        fresh_js = (
            "() => {"
            " const g = document.querySelector('#result');"
            " const m = document.querySelector('#resultMessage');"
            " const msgShown = !!(m && !m.classList.contains('hidden'));"
            " const gridText = g ? g.innerText : '';"
            " const msgText = m ? m.innerText : '';"
            f" const wasGrid = {str(bool(before.get('hasGrid'))).lower()};"
            f" const wasMsg = {str(bool(before.get('msgShown'))).lower()};"
            f" const oldGrid = {json.dumps(before.get('gridText') or '')};"
            f" const oldMsg = {json.dumps(before.get('msgText') or '')};"
            " return (g && (!wasGrid || gridText !== oldGrid))"
            "      || (msgShown && (!wasMsg || msgText !== oldMsg));"
            "}"
        )
        try:
            await self.page.wait_for_function(fresh_js, timeout=15_000)
        except PwTimeout:
            raise SiteNotConfigured(
                f"search for {date_dmy} produced neither a fresh #result nor "
                "a fresh #resultMessage — treating as a failed read, not zero slots"
            )

    async def list_slots(self) -> list[str]:
        """Search each date in the configured window and return one label per
        bookable slot, 'DD/MM/YYYY HH:MM'."""
        dates = self._scan_dates()
        labels: list[str] = []
        self._last_slots = {}
        for dmy in dates:
            await self._search(dmy)
            found = await self._read_grid(dmy)
            labels.extend(found)
        return labels

    async def _read_grid(self, date_dmy: str) -> list[str]:
        rows = await self.page.evaluate(
            "() => [...document.querySelectorAll('#result .appointment_slot')]"
            ".map(e => ({id: e.id || '', time: e.getAttribute('slot-time') || '',"
            "           enabled: e.classList.contains('appointment_slot_enabled')}))"
        )
        out: list[str] = []
        for r in rows:
            if not r["enabled"] or not r["time"]:
                continue
            label = f"{date_dmy} {r['time']}"
            m = re.fullmatch(r"t_(\d+)", r["id"])
            if m:
                self._last_slots[label] = int(m.group(1))
            out.append(label)
        return out

    def _scan_dates(self) -> list[str]:
        """DD/MM/YYYY strings to search, in order."""
        from datetime import date, timedelta

        target = self.client.get("target_date")  # DD/MM/YYYY or YYYY-MM-DD
        if target:
            return [_iso_to_dmy(target) if "-" in target else target]
        days = int(self.client.get("scan_days", 14) or 14)
        days = max(1, min(days, 90))
        start = date.today()
        return [
            (start + timedelta(days=i)).strftime("%d/%m/%Y")
            for i in range(days)
        ]

    # ── select (client-side; does NOT hold the slot server-side) ─────────

    async def locate(self, slot: dict) -> bool:
        """Re-search the slot's date and check an enabled cell for its time."""
        date_dmy = _iso_to_dmy(slot["date"]) if "-" in slot["date"] else slot["date"]
        await self._search(date_dmy)
        return self._slot_el_id(slot) is not None

    def _slot_el_id(self, slot: dict) -> int | None:
        iso = slot["date"] if "-" in slot["date"] else _dmy_to_iso(slot["date"])
        dmy = slot["date"] if "/" in slot["date"] else _iso_to_dmy(slot["date"])
        _ = iso
        return self._last_slots.get(f"{dmy} {slot.get('time') or ''}")

    async def reserve(self, slot: dict) -> None:
        """selectTime() equivalent: click the enabled cell, verify the
        client-side selection landed. Does NOT reserve on the server — the
        real hold happens at #btn-new-appointment."""
        pid = self._slot_el_id(slot)
        if pid is None:
            raise SlotNotFound(f"no enabled slot for {slot.get('date')} {slot.get('time')}")
        sel = f"#t_{pid}"
        el = self.page.locator(f"{sel}{SEL_SLOT_ENABLED}")
        if not await el.count():
            raise SlotNotFound(f"slot {pid} is not selectable")
        await el.click()
        try:
            await self.page.wait_for_selector(
                f"{sel}.{SEL_SLOT_SELECTED}", timeout=5_000
            )
        except PwTimeout:
            raise SlotNotFound(
                f"clicked slot {pid} but selection marker never appeared"
            )

    # ── applicant prefill (private local data, never the repo) ───────────

    async def fill_applicant(self, data: dict) -> None:
        for key, sel in APPLICANT_FIELD_MAP.items():
            val = data.get(key)
            if val:
                await self._jq_set(sel, str(val))
        for key, sel in APPLICANT_SELECT_MAP.items():
            val = data.get(key)
            if val:
                await self._jq_set(sel, str(val))
        if data.get("bookingfor"):
            await self._jq_set(SEL_BOOKINGFOR, str(data["bookingfor"]))

    # ── OTP / captcha / submit ───────────────────────────────────────────

    async def request_otp(self) -> str | None:
        """Click 'Request OTP'. Returns an error string on failure."""
        if not await self._val(SEL_SELECTEDTIME):
            return "no slot selected — pick a slot before requesting OTP"
        await self.page.click(SEL_OTP_BTN)
        try:
            await self.page.wait_for_selector(
                f"{SEL_OTP_WRAP}:not([style*='none']), {SEL_OTP_INPUT}",
                state="visible", timeout=10_000,
            )
        except PwTimeout:
            err = await self._alert_error()
            return err or "OTP prompt did not appear"
        return None

    async def enter_otp(self, code: str) -> None:
        await self._jq_set(SEL_OTP_INPUT, code.strip())

    async def _val(self, sel: str) -> str:
        return await self.page.evaluate(
            "sel => (document.querySelector(sel) || {}).value || ''", sel
        )

    async def _alert_error(self) -> str | None:
        try:
            return await self.page.evaluate(
                "() => { const e = document.querySelector('.alertify .ajs-error');"
                " return e && e.offsetParent ? e.textContent.trim() : null; }"
            )
        except Exception:
            return None

    async def submit(self) -> None:
        """The client-authorized final step: check the consent box and press
        'Book your appointment'. Never auto-called — only the dashboard's
        explicit confirm action reaches this."""
        await self.page.evaluate(
            "() => document.querySelectorAll('.ajs-error')"
            ".forEach(e => e.classList.add('hidden'))"
        )
        box = self.page.locator(SEL_SUBMIT_INFO)
        if await box.count() and not await box.is_checked():
            await box.check()
        await self.page.click(SEL_BOOK_BTN)

    async def submit_outcome(self, timeout_ms: int = 20_000) -> dict:
        """After submit(): classify what the portal did.
        {'ok': ref} on redirect to /appointments/result/{ref},
        {'error': msg} on a visible portal error, {'error': None} on timeout
        (ambiguous — caller must treat as unknown, never as success)."""
        import asyncio
        import time

        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if CONFIRM_URL in self.page.url:
                return {"ok": await self.confirmation_ref()}
            err = await self._alert_error()
            if err:
                return {"error": err}
            await asyncio.sleep(0.4)
        return {"error": None}  # genuinely ambiguous — never a success

    async def confirmation_ref(self) -> str | None:
        if CONFIRM_URL in self.page.url:
            tail = self.page.url.split(CONFIRM_URL, 1)[1].strip("/?# ")
            if tail:
                return tail
        try:
            ref = await self.page.evaluate(
                "() => { const h=[...document.querySelectorAll('h1,h2,h3,.confirmation,.ref')]"
                ".map(e=>e.textContent).find(t=>/GVCW|ref|confirm/i.test(t)); return h||null; }"
            )
            return ref
        except Exception:
            return None

    # ── misc protocol ────────────────────────────────────────────────────

    async def read_tokens(self) -> dict[str, str]:
        return {}

    async def screenshot(self, path: Path) -> None:
        await self.page.screenshot(path=str(path))

    async def content(self) -> str:
        return await self.page.content()

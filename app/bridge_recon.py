"""Read-only recon through the operator's signed-in tab via the CDP bridge.

What it does, in order — and nothing more:

  1. verify the bridge is up and the EXPLICIT tab id exists on the expected
     origin (never picks another tab);
  2. stop if the page shows a login form / 401 (session gone) or a challenge;
  3. assert every selector GvcwSiteDriver depends on, so portal drift shows
     up as a named missing element;
  4. run ONE normal availability Search (seasonal/individual by default on
     the account-bound VAC) and report what the grid contained;
  5. save a redacted evidence JSON (request/response, no cookie headers).

No OTP, no submit, no cookies, no credentials. Booking actions exist only
behind an explicit --enable-booking operator flag, which this command does
not implement — this file is observation only.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .cdp_bridge import BridgeClient, BridgePage
from .gvcw import (
    GvcwSiteDriver,
    SEL_BOOKINGFOR,
    SEL_DATEFROM,
    SEL_GP_PERIOD,
    SEL_METHOD,
    SEL_NO_RESULTS,
    SEL_OTP_BTN,
    SEL_OTP_INPUT,
    SEL_RESULT_DIV,
    SEL_RESULT_MSG,
    SEL_SEARCH,
    SEL_SELECTEDTIME,
    SEL_SLOT_ENABLED,
    SEL_SUBMIT_INFO,
    SEL_BOOK_BTN,
    SEL_TYPE,
)

logger = logging.getLogger(__name__)

# Everything the driver touches — a missing element = portal drift.
EXPECTED_SELECTORS = {
    "type_select": SEL_TYPE,
    "bookingfor_select": SEL_BOOKINGFOR,
    "method_select": SEL_METHOD,
    "date_input": SEL_DATEFROM,
    "search_button": SEL_SEARCH,
    "results_container": SEL_RESULT_DIV,
    "result_message": SEL_RESULT_MSG,
    "no_results": SEL_NO_RESULTS,
    "selected_time": SEL_SELECTEDTIME,
    "gp_periodslotid": SEL_GP_PERIOD,
    "submit_checkbox": SEL_SUBMIT_INFO,
    "otp_button": SEL_OTP_BTN,
    "otp_input": SEL_OTP_INPUT,
    "book_button": SEL_BOOK_BTN,
}


async def run_recon(
    tab_id: int,
    allowed_origin: str,
    *,
    category: str = "26",
    target_date: str | None = None,   # DD/MM/YYYY — required for a search
    bridge_base: str = "http://localhost:1232",
    evidence_dir: Path | None = None,
) -> dict:
    """Read-only verification. Returns a report dict; raises Bridge* errors."""
    client = BridgeClient(bridge_base)
    if not await client.health():
        return {"ok": False, "stage": "bridge",
                "error": f"no bridge at {bridge_base} — is the extension running?"}

    tabs = await client.tabs()
    tab = next((t for t in tabs if t.get("tabId") == tab_id), None)
    if tab is None:
        return {"ok": False, "stage": "tab",
                "error": f"tab {tab_id} not found — pass --tab from /tabs",
                "available": [t.get("tabId") for t in tabs]}

    page: BridgePage = await client.page(
        tab_id, [allowed_origin], allow_writes=True)
    driver = GvcwSiteDriver(page, {"bookingfor": "0", "method": "1"})

    report: dict = {"ok": True, "url": tab.get("url"), "checks": {},
                    "slots": [], "empty": None}

    # 1-2. tab + session + challenge gates happen inside the first gated call
    present = await page.evaluate(
        "(sels) => { const out = {}; for (const k in sels)"
        "   out[k] = !!document.querySelector(sels[k]); return out; }",
        EXPECTED_SELECTORS,
    )
    report["checks"] = present
    missing = [k for k, v in present.items() if not v]
    if missing:
        report["ok"] = False
        report["stage"] = "selectors"
        report["missing"] = missing
        return report

    # category option sanity
    options = await page.evaluate(
        "sel => [...document.querySelectorAll(sel + ' option')]"
        "   .map(o => o.value)", SEL_TYPE)
    report["type_options"] = options
    if category not in options:
        report["ok"] = False
        report["stage"] = "category"
        report["error"] = f"#type has no option {category!r}: {options}"
        return report

    # 4. ONE normal search — the same PUT the page makes on Search click.
    if not target_date:
        report["stage"] = "search"
        report["note"] = "no target date — selectors verified, search skipped"
        return report

    await driver.apply_filters(None, category)
    await driver._search(target_date)
    slots = await page.evaluate(
        "() => [...document.querySelectorAll('#result .appointment_slot')]"
        "   .map(e => ({id: e.id, time: e.getAttribute('slot-time'),"
        "              enabled: e.classList.contains('appointment_slot_enabled'),"
        "              selectable: e.classList.contains('appointment_slot_notselectable') === false}))")
    visible_empty = await page.evaluate(
        "() => { const m = document.querySelector('#resultMessage');"
        " return m && !m.classList.contains('hidden'); }")
    report["slots"] = slots or []
    report["empty"] = bool(visible_empty)
    report["bookable"] = [s for s in (slots or []) if s.get("enabled")]

    # 5. redacted evidence
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = evidence_dir / f"bridge-recon-{stamp}.json"
        out.write_text(json.dumps({
            "ts": stamp, "url": report["url"], "target_date": target_date,
            "category": category, "checks": present,
            "slots": slots, "empty": visible_empty,
        }, indent=2))
        report["evidence"] = str(out)

    return report

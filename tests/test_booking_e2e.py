"""End-to-end booking test against the mock portal, driven by a real browser.

This is the test that proves the booker works: a slot is detected, claimed, the
slot is really gone from the portal afterwards, the alarm fires, and a second
attempt cannot claim it again.

Skipped automatically when Playwright's chromium is not installed.
"""
from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from fake_portal.app import PASSWORD, USERNAME, PortalState, create_app  # noqa: E402

from app.booking import BookingAgent  # noqa: E402
from app.config import (  # noqa: E402
    AppConfig,
    BookingConfig,
    Config,
    NotificationsConfig,
    ProfileConfig,
)
from app.site import BookingSelectors, PlaywrightSiteDriver  # noqa: E402
from app.store import Store  # noqa: E402

PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"
PROFILE_NAME = "Ahmed - Work Permit"


class _RecordingAlarm:
    """Stands in for the real alarm so tests never beep."""

    def __init__(self) -> None:
        self.message: str | None = None
        self.starts = 0

    def start(self, message: str) -> None:
        self.message = message
        self.starts += 1

    def stop(self) -> None:
        pass


def _serve(app, port: int):
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return server
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("mock portal did not start")


@pytest.fixture(scope="module")
def portal():
    state = PortalState()
    server = _serve(create_app(state), PORT)
    yield state
    server.should_exit = True


def _cfg(tmp_path: Path, mode: str) -> Config:
    return Config(
        app=AppConfig(dry_run=False, headless=True),
        profiles=[
            ProfileConfig(
                name=PROFILE_NAME,
                email="a@example.com",
                category="national_d_employment",
                location="lahore",
                booking_url=f"{BASE}/en/appointments",
                auto_book=True,
            )
        ],
        notifications=NotificationsConfig(to=["a@example.com"]),
        booking=BookingConfig(
            mode=mode,
            dry_run=False,
            alarm_seconds=1,
            evidence_dir=tmp_path / "evidence",
            profile_dir=tmp_path / "profile",
            selectors_file=tmp_path / "selectors.json",
        ),
        root=tmp_path,
        data_dir=tmp_path,
        state_dir=tmp_path,
    )


def _selectors() -> BookingSelectors:
    return BookingSelectors(
        vac_select="select[name='vac']",
        category_select="select[name='category']",
        slot_row=".slot-row",
        reserve_button="button.reserve",
        applicant_fields={"passport": "#edit-passport", "fullname": "#edit-fullname"},
        submit_button="#edit-submit",
        confirmation_marker="#confirmation",
    )


def _slot(date: str, time: str) -> dict:
    return {
        "profile": PROFILE_NAME,
        "location": "lahore",
        "category": "national_d_employment",
        "date": date,
        "time": time,
        "raw": f"{date} {time}",
    }


async def _open_driver(pw):
    """Launch chromium and perform the human's one-time sign-in."""
    try:
        browser = await pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - missing browser binary
        pytest.skip(f"chromium unavailable: {exc}")
    page = await browser.new_page()
    await page.goto(f"{BASE}/login/appointments/en")
    await page.fill("#edit-name", USERNAME)
    await page.fill("#edit-pass", PASSWORD)
    await page.click(".form-submit")
    await page.wait_for_url("**/en/account")
    return browser, page


def _run(coro):
    return asyncio.run(coro)


def test_assist_mode_claims_the_slot_and_alarms(tmp_path, portal):
    portal.reset()
    portal.add_slot("2026-12-01", "09:00")

    async def flow():
        from playwright.async_api import async_playwright

        store = Store(tmp_path / "bookings.db")
        alarm = _RecordingAlarm()
        agent = BookingAgent(_cfg(tmp_path, "assist"), _profile_dict(), store, alarm=alarm)

        async with async_playwright() as pw:
            browser, page = await _open_driver(pw)
            try:
                driver = PlaywrightSiteDriver(page, _selectors())
                result = await agent.book(_slot("2026-12-01", "09:00"), driver)
            finally:
                await browser.close()

        row = store.recent_bookings()[0]
        store.close()
        return result, row, alarm

    result, row, alarm = _run(flow())

    # the bot claimed the slot and stopped for a human
    assert result.status == "needs_human", result.error
    assert result.stage == "assist"
    assert alarm.starts == 1 and "SLOT CLAIMED" in (alarm.message or "")
    # the portal really holding it is what makes this a booking, not just a click
    assert len(portal.held) == 1
    assert portal.held[0]["date"] == "2026-12-01"
    assert portal.slots == []
    assert portal.reserve_calls == 1
    assert row["status"] == "needs_human"
    assert result.evidence_path and Path(result.evidence_path).exists()


def test_claiming_the_same_slot_twice_is_refused(tmp_path, portal):
    portal.reset()
    portal.add_slot("2026-12-05", "10:30")

    async def flow():
        from playwright.async_api import async_playwright

        store = Store(tmp_path / "twice.db")
        cfg = _cfg(tmp_path, "assist")
        agent = BookingAgent(cfg, _profile_dict(), store, alarm=_RecordingAlarm())

        async with async_playwright() as pw:
            browser, page = await _open_driver(pw)
            try:
                driver = PlaywrightSiteDriver(page, _selectors())
                first = await agent.book(_slot("2026-12-05", "10:30"), driver)
                second = await agent.book(_slot("2026-12-05", "10:30"), driver)
            finally:
                await browser.close()

        rows = store.recent_bookings()
        store.close()
        return first, second, rows

    first, second, rows = _run(flow())

    assert first.status == "needs_human"
    assert second.status == "skipped_duplicate"
    # the critical guarantee: only ONE reserve ever reached the portal
    assert portal.reserve_calls == 1
    assert len(rows) == 1


def test_vanished_slot_reports_slot_gone_without_reserving(tmp_path, portal):
    portal.reset()  # no slots at all: the picker shows none

    async def flow():
        from playwright.async_api import async_playwright

        store = Store(tmp_path / "gone.db")
        agent = BookingAgent(_cfg(tmp_path, "assist"), _profile_dict(), store, alarm=_RecordingAlarm())

        async with async_playwright() as pw:
            browser, page = await _open_driver(pw)
            try:
                driver = PlaywrightSiteDriver(page, _selectors())
                result = await agent.book(_slot("2026-12-09", "09:30"), driver)
            finally:
                await browser.close()
        row = store.recent_bookings()[0]
        store.close()
        return result, row

    result, row = _run(flow())

    assert result.status == "slot_gone"
    assert result.stage == "locate"
    assert portal.reserve_calls == 0
    assert row["status"] == "slot_gone"


def test_dry_run_never_clicks_reserve(tmp_path, portal):
    portal.reset()
    portal.add_slot("2026-12-11", "09:00")

    async def flow():
        from playwright.async_api import async_playwright

        store = Store(tmp_path / "dry.db")
        cfg = _cfg(tmp_path, "assist")
        cfg.booking.dry_run = True
        agent = BookingAgent(cfg, _profile_dict(), store, alarm=_RecordingAlarm())

        async with async_playwright() as pw:
            browser, page = await _open_driver(pw)
            try:
                driver = PlaywrightSiteDriver(page, _selectors())
                result = await agent.book(_slot("2026-12-11", "09:00"), driver)
            finally:
                await browser.close()
        store.close()
        return result

    result = _run(flow())

    assert result.status == "dry_run"
    assert portal.reserve_calls == 0
    assert portal.slots != []  # slot untouched


def _profile_dict() -> dict:
    return {
        "name": PROFILE_NAME,
        "email": "a@example.com",
        "category": "national_d_employment",
        "location": "lahore",
        "booking_url": f"{BASE}/en/appointments",
        "auto_book": True,
        "prefer": {},
    }

"""Sequential applicant-queue verification on the faithful mock.

LocalMonitor drives a real Playwright page (the transport is interchangeable
— bridge paths are covered elsewhere). Proves: two applicants process in
order and only advance on a VERIFIED booking; a restart preserves queue
state; an ambiguous submit parks the queue and never reprocesses.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_portal.app import PortalState, create_app, USERNAME, PASSWORD  # noqa: E402
from test_hosted_e2e import _serve  # noqa: E402
from app.localapp import LocalMonitor  # noqa: E402
from app.queue import ApplicantQueue  # noqa: E402


def _app1():
    return {"surname": "AHMED", "firstname": "SARA",
            "dateofbirth": "01/01/1990", "passportnumber": "AB1234567",
            "traveldocumentvaliduntil": "01/01/2030",
            "email": "sara@example.com", "phonenumber": "3001111111",
            "nationality": "Pakistani", "gender": "female",
            "prefix": "30"}


def _app2():
    return {"surname": "KHAN", "firstname": "OMAR",
            "dateofbirth": "02/02/1992", "passportnumber": "CD7654321",
            "traveldocumentvaliduntil": "01/01/2031",
            "email": "omar@example.com", "phonenumber": "3002222222",
            "nationality": "Pakistani", "gender": "male",
            "prefix": "30"}


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("localapp")
    portal_state = PortalState()
    server, port, _ = _serve(create_app(portal_state))
    base = f"http://127.0.0.1:{port}"
    loop = asyncio.new_event_loop()

    async def _build():
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        return pw, browser

    pw, browser = loop.run_until_complete(_build())
    yield base, browser, loop, portal_state, tmp
    server.should_exit = True
    loop.run_until_complete(browser.close())
    loop.run_until_complete(pw.stop())
    loop.close()


async def _signed_page(browser, base):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base}/login/appointments/en")
    await page.fill("#edit-name", USERNAME)
    await page.fill("#edit-pass", PASSWORD)
    await page.click("button[type=submit]")
    await page.goto(f"{base}/appointments/add")
    return ctx, page


def _seed(portal_state, base):
    import httpx
    httpx.post(f"{base}/__admin__/gvcw-slots",
               json={"date": "30/11/2026", "time": "09:00"}, timeout=5).raise_for_status()


def test_sequential_booking_and_restart(rig):
    base, browser, loop, portal, tmp = rig
    portal.reset()
    queue = ApplicantQueue(tmp / "queue.db")
    queue.add(_app1(), "Sara Ahmed", category="26",
              date_from="30/11/2026", date_to="30/11/2026")
    queue.add(_app2(), "Omar Khan", category="26",
              date_from="30/11/2026", date_to="30/11/2026")

    async def flow():
        ctx, page = await _signed_page(browser, base)
        try:
            monitor = LocalMonitor(queue, lambda: asyncio.sleep(0, page), base)
            monitor.pair(1)
            _seed(portal, base)
            a1 = queue.current()
            assert a1["display_name"] == "Sara Ahmed"
            await monitor._cycle(a1)
            assert queue.get(a1["id"])["status"] == "selected_awaiting_human"

            # OTP + explicit confirm — the operator path
            assert (await monitor.request_otp())["ok"]
            assert (await monitor.submit_otp(portal.gvcw_otp))["ok"]
            out = await monitor.confirm_book()
            assert out["ok"], out
            assert out["confirmation_ref"].startswith("GVCW-")
            assert queue.get(a1["id"])["status"] == "booked"
            # verified success navigates the SAME page back to the form —
            # not a fresh context — so the next applicant continues on it
            assert page.url.endswith("/appointments/add"), page.url

            # applicant 2 becomes current — sequential advance
            a2 = queue.current()
            assert a2["display_name"] == "Omar Khan"
            portal.gvcw_slots.clear()
            _seed(portal, base)
            await page.goto(f"{base}/appointments/add")
            await monitor._cycle(a2)
            assert queue.get(a2["id"])["status"] == "selected_awaiting_human"
        finally:
            await ctx.close()

    loop.run_until_complete(flow())

    # restart persistence: a fresh queue object on the same DB sees progress
    queue2 = ApplicantQueue(tmp / "queue.db")
    statuses = {q["display_name"]: q["status"] for q in queue2.list()}
    assert statuses["Sara Ahmed"] == "booked"
    assert statuses["Omar Khan"] == "selected_awaiting_human"
    queue2.close()


def test_ambiguous_submit_parks_queue(rig):
    base, browser, loop, portal, tmp = rig
    portal.reset()
    queue = ApplicantQueue(tmp / "queue2.db")
    queue.add(_app1(), "Sara Ahmed", category="26",
              date_from="30/11/2026", date_to="30/11/2026")

    async def flow():
        ctx, page = await _signed_page(browser, base)
        try:
            monitor = LocalMonitor(queue, lambda: asyncio.sleep(0, page), base)
            monitor.pair(1)
            _seed(portal, base)
            a1 = queue.current()
            await monitor._cycle(a1)
            assert (await monitor.request_otp())["ok"]
            assert (await monitor.submit_otp(portal.gvcw_otp))["ok"]
            portal.gvcw_fail_next_submit = "timeout"   # ambiguous
            out = await monitor.confirm_book()
            assert not out["ok"]
            assert queue.get(a1["id"])["status"] == "outcome_unknown"
            # never advances, never reprocessed
            assert queue.current()["id"] == a1["id"]
        finally:
            await ctx.close()

    loop.run_until_complete(flow())


def test_duplicate_identity_rejected(rig):
    base, browser, loop, portal, tmp = rig
    queue = ApplicantQueue(tmp / "queue3.db")
    queue.add(_app1(), "Sara Ahmed")
    with pytest.raises(ValueError):
        queue.add(_app1(), "Sara Ahmed clone")   # same surname+passport
    queue.close()

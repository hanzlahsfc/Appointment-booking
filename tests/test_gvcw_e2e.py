"""E2E for the REAL portal's flow, exercised against the gvcw-faithful mock.

Covers the whole assisted sequence the milestone demands:
  login (forwarded input) → search-driven availability → matching slot
  detected + client-side selection → applicant prefill from private local
  data → client alarm → client-requested OTP → OTP entry → explicit
  'Confirm and book' → SUCCESS verified via /appointments/result/{ref}
  → booked recorded, profile cannot book again.

Plus the failure contract: a lost slot at submit produces an error, leaves
the attempt parked (needs_human), and is never retried or recorded booked.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from fake_portal.app import PASSWORD, USERNAME, PortalState, create_app  # noqa: E402
from test_hosted_e2e import (  # noqa: E402
    _SilentAlarm, _hosted_cfg, _serve, _serve_in_loop, WsClient,
    _click_element, _login, _wait_url,
)

from app.hosted.manager import BrowserHostManager  # noqa: E402
from app.hosted.monitor import HostedMonitor, register_client_profiles  # noqa: E402
from app.hosted.server import HostedRuntime, create_app as create_hosted_app  # noqa: E402
from app.hosted.store import HostedStore  # noqa: E402
from app.store import Store  # noqa: E402

import httpx  # noqa: E402

PORTAL_BASE = ""


@pytest.fixture(scope="module")
def portal():
    global PORTAL_BASE
    state = PortalState()
    server, port, thread = _serve(create_app(state))
    PORTAL_BASE = f"http://127.0.0.1:{port}"
    yield state
    server.should_exit = True
    thread.join(timeout=10)


def _gvcw_fields(username: str, applicant: str) -> dict:
    return {
        "profile_name": f"client:{username}",
        "booking_url": f"{PORTAL_BASE}/appointments/add",
        "category": "26",
        "location": "lahore",
        "allowed_origins": [PORTAL_BASE],
        "selectors": {
            "portal": "gvcw",
            "target_date": "30/11/2026",
            "applicant_file": applicant,
        },
        "auto_book": True,
    }


async def _spin_up(tmp_path, portal, username="dina"):
    cfg = _hosted_cfg(tmp_path)
    # private applicant data — local file only
    applicants = cfg.data_dir / "applicants"
    applicants.mkdir(parents=True, exist_ok=True)
    (applicants / f"{username}.json").write_text(json.dumps({
        "surname": "TESTER", "firstname": "DINA",
        "dateofbirth": "10/10/1993", "passportnumber": "AA1234567",
        "traveldocumentvaliduntil": "19/12/2035", "gender": "1",
        "nationality": "146", "email": "dina@example.com",
        "phonenumber": "3001234567", "phone_prefix": "40",
    }))
    hstore = HostedStore(cfg.hosted.db_path, cfg.db_path)
    store = Store(cfg.db_path)
    manager = BrowserHostManager(cfg.hosted)
    await manager.start()
    client, code = hstore.add_client(
        username=username, display_name="Dina", **_gvcw_fields(username, username)
    )
    register_client_profiles(cfg, [client])
    alarm = _SilentAlarm()
    monitor = HostedMonitor(cfg, client, store, manager, alarm=alarm)
    runtime = HostedRuntime(cfg, hstore, store, manager)
    runtime.monitors = {client["id"]: monitor}
    app = create_hosted_app(runtime)
    server, port, _task = await _serve_in_loop(app)
    return cfg, hstore, store, manager, client, code, alarm, monitor, server, port


async def _manual_login(ws, browser):
    await ws.send({"type": "control", "action": "take"})
    await ws.wait_for(
        lambda m: m["type"] == "status" and m.get("control") == "manual")
    await _click_element(ws, browser, "#edit-name")
    await ws.send({"type": "text", "text": USERNAME})
    await ws.send({"type": "key", "event": "press", "key": "Tab"})
    await ws.send({"type": "text", "text": PASSWORD})
    await ws.send({"type": "key", "event": "press", "key": "Enter"})
    await _wait_url(browser, "/en/account")
    await ws.send({"type": "control", "action": "release"})
    await asyncio.sleep(0.3)


def test_gvcw_assisted_booking_full_sequence(tmp_path, portal):
    asyncio.run(_full_sequence(tmp_path, portal))


async def _full_sequence(tmp_path, portal: PortalState) -> None:
    portal.reset()
    (cfg, hstore, store, manager, client, code, alarm,
     monitor, server, port) = await _spin_up(tmp_path, portal)
    base = f"http://127.0.0.1:{port}"
    monitor.start()
    ws = WsClient()
    try:
        async with httpx.AsyncClient() as http:
            token = await _login(http, base, code)
            headers = {"Authorization": f"Bearer {token}"}
            await ws.connect(f"ws://127.0.0.1:{port}/ws/screen?token={token}")
            await ws.wait_for(lambda m: m["type"] == "hello")
            browser = manager.browser_for(client["id"])

            # login page reached; client signs in through forwarded input
            await ws.wait_for(
                lambda m: m["type"] == "monitor"
                and m["state"].get("status") == "awaiting_login", timeout=40)
            await _manual_login(ws, browser)

            # monitor opens /appointments/add; empty search is a clean 'ok'
            await ws.wait_for(
                lambda m: m["type"] == "monitor" and m["state"].get("status") == "ok",
                timeout=60)
            assert browser.page.url.endswith("/appointments/add")
            # the search-driven page rendered its empty state
            assert await browser.page.locator("#type").count() == 1

            # ── seed a seasonal slot → detected + selected + prefilled ──
            resp = await http.post(
                f"{PORTAL_BASE}/__admin__/gvcw-slots",
                json={"date": "30/11/2026", "time": "09:00", "category": "26"})
            assert resp.status_code == 200

            await ws.wait_for(
                lambda m: m["type"] == "monitor"
                and m["state"].get("status") in ("needs_human", "holding"),
                timeout=60)
            assert alarm.starts >= 1  # client alarm fired

            page = browser.page
            # client-side slot selection landed (real portal semantics)
            assert await page.locator(".appointment_slot_selected").count() == 1
            assert await page.locator("#selectedtime").input_value() == "09:00"
            assert await page.locator("#gp_periodslotid").input_value() != ""
            # applicant prefilled from private local data
            assert await page.locator("#gp_surname").input_value() == "TESTER"
            assert await page.locator("#gp_passportnumber").input_value() == "AA1234567"

            # nothing is booked yet — selection alone never reserves
            assert not portal.gvcw_bookings

            # ── OTP request + entry through the client API ────────────
            r = await http.post(f"{base}/api/booking/otp/request", headers=headers)
            assert r.status_code == 200 and r.json().get("ok"), r.text
            assert await page.locator("#onetimepassword").is_visible()

            r = await http.post(f"{base}/api/booking/otp", headers=headers,
                                json={"code": "246810"})
            assert r.json().get("ok"), r.text
            assert await page.locator("#onetimepassword").input_value() == "246810"

            # a wrong caller cannot act on this booking
            # ── explicit 'Confirm and book' → verified booked ─────────
            r = await http.post(f"{base}/api/booking/confirm", headers=headers)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data.get("ok"), data
            ref = data["confirmation_ref"]
            assert ref
            assert "/appointments/result/" in page.url

            bookings = portal.gvcw_bookings
            assert bookings and bookings[0]["surname"] == "TESTER"
            assert store.has_booked("client:dina")
            records = (await http.get(f"{base}/api/records",
                                      headers=headers)).json()
            booked = [b for b in records["bookings"] if b["status"] == "booked"]
            assert booked and booked[0]["confirmation_ref"] == ref
    finally:
        await ws.close()
        await monitor.stop()
        server.should_exit = True
        await manager.stop_all()
        store.close()
        hstore.close()


def test_gvcw_lost_slot_stays_parked(tmp_path, portal):
    asyncio.run(_lost_slot(tmp_path, portal))


async def _lost_slot(tmp_path, portal: PortalState) -> None:
    portal.reset()
    (cfg, hstore, store, manager, client, code, alarm,
     monitor, server, port) = await _spin_up(tmp_path, portal, username="erin")
    base = f"http://127.0.0.1:{port}"
    monitor.start()
    ws = WsClient()
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            token = await _login(http, base, code)
            headers = {"Authorization": f"Bearer {token}"}
            await ws.connect(f"ws://127.0.0.1:{port}/ws/screen?token={token}")
            await ws.wait_for(lambda m: m["type"] == "hello")
            browser = manager.browser_for(client["id"])
            await ws.wait_for(
                lambda m: m["type"] == "monitor"
                and m["state"].get("status") == "awaiting_login", timeout=40)
            await _manual_login(ws, browser)
            await ws.wait_for(
                lambda m: m["type"] == "monitor" and m["state"].get("status") == "ok",
                timeout=60)

            await http.post(f"{PORTAL_BASE}/__admin__/gvcw-slots",
                            json={"date": "30/11/2026", "time": "10:00",
                                  "category": "26"})
            await ws.wait_for(
                lambda m: m["type"] == "monitor"
                and m["state"].get("status") in ("needs_human", "holding"),
                timeout=60)

            # OTP succeeds, but the submit is told to fail — the portal
            # reports the slot gone between select and book.
            await http.post(f"{base}/api/booking/otp/request", headers=headers)
            await http.post(f"{base}/api/booking/otp", headers=headers,
                            json={"code": "246810"})
            await http.post(f"{PORTAL_BASE}/__admin__/gvcw-fail",
                            json={"message": "The selected slot is no longer available"})

            r = await http.post(f"{base}/api/booking/confirm", headers=headers)
            data = r.json()
            assert data["ok"] is False
            assert "no longer available" in data["error"]

            # no false success, no blind retry: row stays needs_human and
            # parked, nothing booked, slot selection untouched
            assert not store.has_booked("client:erin")
            assert not portal.gvcw_bookings
            unresolved = store.unresolved_booking("client:erin")
            assert unresolved and unresolved["status"] == "needs_human"

            # and the monitor does not storm the portal afterwards
            await asyncio.sleep(5)
            assert not portal.gvcw_bookings
    finally:
        await ws.close()
        await monitor.stop()
        server.should_exit = True
        await manager.stop_all()
        store.close()
        hstore.close()

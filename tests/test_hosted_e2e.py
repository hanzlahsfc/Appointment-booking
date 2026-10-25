"""End-to-end hosted-client test: the whole milestone on real browsers.

Against the fake portal this proves, in one run:

  * provisioned login + session auth (and that anonymous callers get 401/4401)
  * the client sees ONLY their own browser over the screencast
  * validated input (click/type/Tab/Enter) performs a real portal login
  * monitoring polls while idle and pauses during manual control
  * assist booking claims a seeded slot and hands off to the remote client
  * the client finishes the form manually; the reservation persists
  * polling stays parked on the unresolved booking across a monitor restart
  * a second client cannot see the first's browser, records, or stream

Skipped automatically when Playwright's chromium is not installed.
"""
from __future__ import annotations

import asyncio
import json
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

from app.config import (  # noqa: E402
    AppConfig,
    BookingConfig,
    Config,
    HostedConfig,
    NotificationsConfig,
)
from app.hosted.manager import BrowserHostManager  # noqa: E402
from app.hosted.monitor import HostedMonitor, register_client_profiles  # noqa: E402
from app.hosted.presets import mock_preset  # noqa: E402
from app.hosted.server import HostedRuntime, create_app as create_hosted_app  # noqa: E402
from app.hosted.store import HostedStore  # noqa: E402
from app.store import Store  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from websockets.asyncio.client import connect as ws_connect  # noqa: E402

PORTAL_BASE = ""  # filled by the portal fixture
ALICE_PW = "alice-pass-1234"
BOB_PW = "bob-pass-5678"


class _SilentAlarm:
    def __init__(self):
        self.starts = 0
        self.message = None

    def start(self, message):
        self.starts += 1
        self.message = message

    def stop(self):
        pass


def _serve(app):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                       access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        if server.started and server.servers:
            port = server.servers[0].sockets[0].getsockname()[1]
            return server, port, thread
        time.sleep(0.05)
    server.should_exit = True
    raise RuntimeError("server did not start")


@pytest.fixture(scope="module")
def portal():
    global PORTAL_BASE
    state = PortalState()
    server, port, thread = _serve(create_app(state))
    PORTAL_BASE = f"http://127.0.0.1:{port}"
    yield state
    server.should_exit = True
    thread.join(timeout=10)


def _hosted_cfg(tmp_path: Path) -> Config:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return Config(
        app=AppConfig(dry_run=False, headless=True, poll_interval_seconds=300),
        profiles=[],
        notifications=NotificationsConfig(),
        booking=BookingConfig(
            mode="assist", dry_run=False, alarm_seconds=1,
            evidence_dir=tmp_path / "evidence",
            profile_dir=tmp_path / "chrome",
            selectors_file=tmp_path / "sel.json",
        ),
        hosted=HostedConfig(
            enabled=True,
            db_path=tmp_path / "hosted.db",
            browsers_dir=tmp_path / "browsers",
            session_ttl_hours=12,
            manual_idle_seconds=900,
            poll_interval_seconds=2,
            headless=True,
            allow_mock_portal=True,
            mock_portal_url=PORTAL_BASE,
            viewport_width=1024,
            viewport_height=700,
            screen_max_width=1024,
            screen_max_height=700,
        ),
        root=tmp_path,
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )


async def _serve_in_loop(app):
    """Bind uvicorn on port 0 inside the current loop; return (server, port)."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                       access_log=False)
    )
    task = asyncio.create_task(server.serve())
    deadline = time.time() + 20
    while time.time() < deadline:
        if server.started and server.servers:
            port = server.servers[0].sockets[0].getsockname()[1]
            return server, port, task
        await asyncio.sleep(0.05)
    server.should_exit = True
    raise RuntimeError("hosted server did not start")


class WsClient:
    """Collects stream messages in the background; wait_for finds them."""

    def __init__(self):
        self.ws = None
        self.msgs: list[dict] = []
        self._task: asyncio.Task | None = None
        self._cond = asyncio.Condition()

    async def connect(self, url: str) -> None:
        self.ws = await ws_connect(url)
        self._task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                async with self._cond:
                    self.msgs.append(json.loads(raw))
                    self._cond.notify_all()
        except Exception:
            pass

    async def wait_for(self, pred, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for m in self.msgs:
                if pred(m):
                    return m
            async with self._cond:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        raise AssertionError(
            f"timed out waiting for stream event; saw "
            f"{[m.get('type') for m in self.msgs][-15:]}"
        )

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj))

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.ws:
            await self.ws.close()


async def _login(http, base, username, password):
    resp = await http.post(f"{base}/api/login",
                           json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def _click_element(ws: WsClient, browser, selector: str) -> None:
    """Click the centre of an element by forwarding real mouse events.

    The lookup is done server-side (test harness only) — a client would find
    the spot on their screen; what's proven here is that forwarded clicks land.
    """
    box = await browser.page.locator(selector).first.bounding_box()
    assert box, f"{selector} not on page"
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    await ws.send({"type": "mouse", "event": "click", "x": x, "y": y,
                   "button": "left"})
    await asyncio.sleep(0.4)


async def _wait_url(browser, needle: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if browser.page is not None and needle in browser.page.url:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"page never reached {needle!r}; url={browser.page.url}")


def test_hosted_client_flow_end_to_end(tmp_path, portal):
    asyncio.run(_flow(tmp_path, portal))


async def _flow(tmp_path: Path, portal: PortalState) -> None:
    portal.reset()
    cfg = _hosted_cfg(tmp_path)
    hstore = HostedStore(cfg.hosted.db_path, cfg.db_path)
    store = Store(cfg.db_path)
    manager = BrowserHostManager(cfg.hosted)
    await manager.start()

    alice = hstore.add_client(
        username="alice", display_name="Alice", password=ALICE_PW,
        **mock_preset(PORTAL_BASE, "alice"),
    )
    bob = hstore.add_client(
        username="bob", display_name="Bob", password=BOB_PW,
        **mock_preset(PORTAL_BASE, "bob"),
    )
    register_client_profiles(cfg, [alice, bob])
    assert cfg.booking_enabled_for("client:alice")

    alarm = _SilentAlarm()
    monitors = {
        c["id"]: HostedMonitor(cfg, c, store, manager, alarm=alarm)
        for c in (alice, bob)
    }
    runtime = HostedRuntime(cfg, hstore, store, manager)
    runtime.monitors = monitors
    app = create_hosted_app(runtime)
    server, port, _task = await _serve_in_loop(app)
    base = f"http://127.0.0.1:{port}"

    for m in monitors.values():
        m.start()

    alice_ws = WsClient()
    try:
        async with httpx.AsyncClient() as http:
            # ── auth surface ──────────────────────────────────────────
            bad = await http.post(f"{base}/api/login",
                                  json={"username": "alice", "password": "wrong"})
            assert bad.status_code == 401
            noauth = await http.get(f"{base}/api/records")
            assert noauth.status_code == 401

            token_a = await _login(http, base, "alice", ALICE_PW)
            token_b = await _login(http, base, "bob", BOB_PW)
            headers_a = {"Authorization": f"Bearer {token_a}"}
            headers_b = {"Authorization": f"Bearer {token_b}"}

            # ── client A connects, sees her own browser ───────────────
            await alice_ws.connect(f"ws://127.0.0.1:{port}/ws/screen?token={token_a}")
            hello = await alice_ws.wait_for(lambda m: m["type"] == "hello")
            assert hello["client"]["username"] == "alice"
            browser_a = manager.browser_for(alice["id"])
            assert browser_a is not None

            # monitor navigates to the portal, lands on the login page
            await alice_ws.wait_for(
                lambda m: m["type"] == "monitor"
                and m["state"].get("status") == "awaiting_login",
                timeout=40,
            )
            assert "/login/" in browser_a.page.url
            # screencast frames really flow
            frame = await alice_ws.wait_for(lambda m: m["type"] == "frame")
            assert frame["data"]

            # ── manual login through forwarded input ──────────────────
            await alice_ws.send({"type": "control", "action": "take"})
            await alice_ws.wait_for(
                lambda m: m["type"] == "status" and m.get("control") == "manual"
            )
            assert browser_a.is_manual

            await _click_element(alice_ws, browser_a, "#edit-name")
            await alice_ws.send({"type": "text", "text": USERNAME})
            await asyncio.sleep(0.3)
            await alice_ws.send({"type": "key", "event": "press", "key": "Tab"})
            await asyncio.sleep(0.3)
            await alice_ws.send({"type": "text", "text": PASSWORD})
            await asyncio.sleep(0.3)
            await alice_ws.send({"type": "key", "event": "press", "key": "Enter"})

            await _wait_url(browser_a, "/en/account")
            await alice_ws.send({"type": "control", "action": "release"})
            await asyncio.sleep(0.3)
            # input after release is rejected
            count = len(alice_ws.msgs)
            await alice_ws.send({"type": "text", "text": "x"})
            await asyncio.sleep(0.5)
            errs = [m for m in alice_ws.msgs[count:] if m.get("type") == "error"]
            assert errs, "input without manual control should be rejected"

            # ── monitoring resumes and finds nothing yet ──────────────
            await alice_ws.wait_for(
                lambda m: m["type"] == "monitor" and m["state"].get("status") == "ok",
                timeout=40,
            )

            # ── seed a slot → assist claims it → needs_human ──────────
            resp = await http.post(
                f"{PORTAL_BASE}/__admin__/slots",
                json={"date": "2026-12-01", "time": "09:00"},
            )
            assert resp.status_code == 200

            await alice_ws.wait_for(
                lambda m: m["type"] == "monitor"
                and m["state"].get("status") in ("needs_human", "holding"),
                timeout=40,
            )
            assert alarm.starts >= 1
            assert len(portal.held) == 1 and portal.held[0]["date"] == "2026-12-01"
            assert portal.reserve_calls == 1

            # ── client finishes the form manually ─────────────────────
            await alice_ws.send({"type": "control", "action": "take"})
            await alice_ws.wait_for(
                lambda m: m["type"] == "status" and m.get("control") == "manual",
                timeout=10,
            )
            await _click_element(alice_ws, browser_a, "#edit-passport")
            await alice_ws.send({"type": "text", "text": "AB1234567"})
            await asyncio.sleep(0.3)
            await alice_ws.send({"type": "key", "event": "press", "key": "Tab"})
            await asyncio.sleep(0.3)
            await alice_ws.send({"type": "text", "text": "Alice Applicant"})
            await asyncio.sleep(0.3)
            await _click_element(alice_ws, browser_a, "#edit-submit")
            deadline = time.monotonic() + 15
            while True:
                if await browser_a.page.locator("#confirmation").count():
                    break
                if time.monotonic() > deadline:
                    raise AssertionError(
                        f"no confirmation marker; url={browser_a.page.url}"
                    )
                await asyncio.sleep(0.2)

            state = await http.get(f"{PORTAL_BASE}/__admin__/state")
            bookings = state.json()["bookings"]
            assert bookings and bookings[0]["fullname"] == "Alice Applicant"

            # ── records show alice's booking; input now rejected ──────
            await alice_ws.send({"type": "control", "action": "release"})
            records_a = (
                await http.get(f"{base}/api/records", headers=headers_a)
            ).json()
            assert any(b["status"] == "needs_human" for b in records_a["bookings"])
            assert any(s["date"] == "2026-12-01" for s in records_a["slots"])

            # ── restart persistence: unresolved booking parks polling ─
            open_or_unresolved = store.unresolved_booking("client:alice")
            assert open_or_unresolved is not None
            page_url = browser_a.page.url

            # A "fresh" monitor (as after a restart) must refuse to navigate.
            monitor2 = HostedMonitor(cfg, alice, store, manager, alarm=_SilentAlarm())
            monitor2.feed = monitors[alice["id"]].feed
            await monitor2._poll_once(browser_a, _selectors_for(alice))
            assert browser_a.page.url == page_url  # never navigated away

            # ── client B isolation ────────────────────────────────────
            me_b = (await http.get(f"{base}/api/me", headers=headers_b)).json()
            assert me_b["client"]["username"] == "bob"
            records_b = (
                await http.get(f"{base}/api/records", headers=headers_b)
            ).json()
            assert records_b["bookings"] == []
            assert all(
                s["date"] != "2026-12-01" for s in records_b["slots"]
            )

            bob_ws = WsClient()
            await bob_ws.connect(
                f"ws://127.0.0.1:{port}/ws/screen?token={token_b}"
            )
            hello_b = await bob_ws.wait_for(lambda m: m["type"] == "hello")
            assert hello_b["client"]["profile_name"] == "client:bob"
            browser_b = manager.browser_for(bob["id"])
            assert browser_b is not browser_a
            # bob takes control — only his browser flips
            await bob_ws.send({"type": "control", "action": "take"})
            await bob_ws.wait_for(
                lambda m: m["type"] == "status" and m.get("control") == "manual"
            )
            assert browser_b.is_manual and not browser_a.is_manual
            await bob_ws.close()

            # anonymous / bad-token WS must be refused before any data flows
            for url in (
                f"ws://127.0.0.1:{port}/ws/screen",
                f"ws://127.0.0.1:{port}/ws/screen?token=deadbeef",
            ):
                try:
                    async with ws_connect(url) as anon:
                        await asyncio.wait_for(anon.recv(), timeout=5)
                        raise AssertionError("unauthenticated stream stayed open")
                except AssertionError:
                    raise
                except Exception:
                    pass  # handshake rejected / closed — correct
    finally:
        await alice_ws.close()
        for m in monitors.values():
            await m.stop()
        server.should_exit = True
        await manager.stop_all()
        store.close()
        hstore.close()


def _selectors_for(client):
    from app.site import BookingSelectors

    return BookingSelectors(
        **{
            k: (tuple(v) if k == "token_inputs" else v)
            for k, v in client["selectors"].items()
            if k in BookingSelectors.__dataclass_fields__
        }
    )


def test_nav_guard_blocks_disallowed_navigation(tmp_path, portal):
    """The hosted browser cannot be steered off the client's portal origins."""
    asyncio.run(_nav_guard_flow(tmp_path))


async def _nav_guard_flow(tmp_path: Path) -> None:
    cfg = _hosted_cfg(tmp_path)
    hstore = HostedStore(cfg.hosted.db_path, cfg.db_path)
    manager = BrowserHostManager(cfg.hosted)
    await manager.start()
    try:
        client = hstore.add_client(
            username="carol", display_name="Carol", password="carol-pass-99",
            **mock_preset(PORTAL_BASE, "carol"),
        )
        browser = await manager.ensure_browser(client)
        page = await browser.ensure_page()
        await page.goto(client["booking_url"], wait_until="domcontentloaded")
        assert PORTAL_BASE in page.url

        # A disallowed navigation is aborted and the tab steers back.
        try:
            await page.goto("https://evil.example.com/", timeout=5_000)
        except Exception:
            pass  # aborted navigation raises in Playwright — expected
        await asyncio.sleep(1.0)
        assert any("evil.example.com" in u for u in browser.blocked)
        assert PORTAL_BASE in page.url or "chrome-error" in page.url
    finally:
        await manager.stop_all()
        hstore.close()


def test_disallowed_without_flag(tmp_path, portal):
    """With allow_mock_portal OFF the mock origin is refused even for a client
    whose row was provisioned against it — the test-only allowance is live."""
    cfg = _hosted_cfg(tmp_path)
    cfg.hosted.allow_mock_portal = False

    async def check():
        from app.hosted.manager import ClientBrowser
        browser = ClientBrowser(
            {"id": 1, "username": "x",
             "allowed_origins": [PORTAL_BASE],
             "booking_url": f"{PORTAL_BASE}/en/appointments"},
            cfg.hosted,
        )
        assert not browser._allowed(f"{PORTAL_BASE}/en/appointments")
        cfg2 = _hosted_cfg(tmp_path)
        browser.cfg = cfg2.hosted
        assert browser._allowed(f"{PORTAL_BASE}/en/appointments")

    asyncio.run(check())

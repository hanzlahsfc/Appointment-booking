"""BridgePage + bridge-recon against the faithful mock through the real
HTTP transport: a fake bridge server exposing the extension's exact API
(/health, /tabs, /command with Runtime.evaluate/Page.navigate/Input/
Page.captureScreenshot) backed by a real Playwright page on the mock portal.

Proves: read-only recon (selectors + one search), explicit-tab-only
behavior, wrong-origin rejection, disconnect and session-loss stops — and
that nothing mutating runs without allow_writes.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from fake_portal.app import PortalState, create_app  # noqa: E402
from test_hosted_e2e import _serve, _serve_in_loop  # noqa: E402

from app.cdp_bridge import (  # noqa: E402
    BridgeClient, BridgeDisconnected, BridgeNavRejected,
    BridgeReadOnly, BridgeSessionLost,
)
from app.bridge_recon import run_recon  # noqa: E402

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

PORTAL_BASE = ""
BRIDGE_BASE = ""


class FakeBridge:
    """The extension's HTTP contract, backed by one Playwright page."""

    def __init__(self):
        self.app = FastAPI()
        self.tabs: dict[int, object] = {}   # tabId -> playwright Page
        self.app.get("/health")(self._health)
        self.app.get("/tabs")(self._tabs)
        self.app.post("/command")(self._command)

    async def _health(self):
        return {"ok": True}

    async def _tabs(self):
        return {"result": [
            {"tabId": tid, "url": p.url, "title": await p.title()}
            for tid, p in self.tabs.items()
        ]}

    async def _command(self, request: Request):
        body = await request.json()
        page = self.tabs.get(body["tabId"])
        if page is None or page.is_closed():
            return JSONResponse({"error": "Tab not attached"})
        method, params = body["method"], body.get("params") or {}
        try:
            if method == "Runtime.evaluate":
                value = await page.evaluate(
                    params["expression"], params.get("arguments", [None])[0]
                    if params.get("arguments") else None)
                return {"result": {"result": {"type": "object", "value": value}}}
            if method == "Page.navigate":
                await page.goto(params["url"], wait_until="domcontentloaded")
                return {"result": {"frameId": "x"}}
            if method == "Input.dispatchMouseEvent":
                await page.mouse.click(params["x"], params["y"])
                return {"result": {}}
            if method == "Page.captureScreenshot":
                import base64
                png = await page.screenshot()
                return {"result": {"data": base64.b64encode(png).decode()}}
            return JSONResponse({"error": f"unsupported {method}"})
        except Exception as exc:
            return {"result": {"exceptionDetails": {"text": str(exc)}}}


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    """Fake portal + fake bridge + a signed-in page, all on loopback."""
    from playwright.async_api import async_playwright

    async def _build():
        global PORTAL_BASE, BRIDGE_BASE
        portal_state = PortalState()
        portal_server, pport, _ = _serve(create_app(portal_state))
        PORTAL_BASE = f"http://127.0.0.1:{pport}"
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        bridge = FakeBridge()
        # in-loop: bridge handlers await Playwright objects owned by this loop
        bridge_server, bport, bridge_task = await _serve_in_loop(bridge.app)
        BRIDGE_BASE = f"http://127.0.0.1:{bport}"
        return (portal_state, portal_server, pw, browser, bridge,
                bridge_server, bridge_task)

    loop = asyncio.new_event_loop()
    ctx = loop.run_until_complete(_build())
    (portal_state, portal_server, pw, browser, bridge,
     bridge_server, bridge_task) = ctx
    yield portal_state, browser, bridge, loop
    portal_server.should_exit = True
    bridge_server.should_exit = True
    bridge_task.cancel()
    loop.run_until_complete(browser.close())
    loop.run_until_complete(pw.stop())
    loop.close()


async def _signed_page(browser, portal: PortalState, url: str):
    ctx = await browser.new_context()
    sid = "sid-test123"
    portal.sessions[sid] = {"token": "t"}
    await ctx.add_cookies([
        {"name": "sid", "value": sid, "url": PORTAL_BASE, "httpOnly": True}])
    page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    return ctx, page


def test_recon_full_and_guardrails(rig):
    portal, browser, bridge, loop = rig
    loop.run_until_complete(_recon_flow(portal, browser, bridge))


async def _recon_flow(portal: PortalState, browser, bridge: FakeBridge) -> None:
    portal.reset()
    portal.add_gvcw_slot("30/11/2026", "09:00", category="26")
    ctx, page = await _signed_page(browser, portal, f"{PORTAL_BASE}/appointments/add")
    bridge.tabs[42] = page
    try:
        # wrong-tab / missing tab rejection first
        report = await run_recon(999, PORTAL_BASE, bridge_base=BRIDGE_BASE)
        assert report["ok"] is False and report["stage"] == "tab"
        assert 42 in report["available"]

        # wrong-origin gate: allowlist mismatch must stop before any eval
        from app.cdp_bridge import BridgeNavRejected as BNR
        try:
            await run_recon(42, "https://other.example", bridge_base=BRIDGE_BASE)
            raise AssertionError("off-origin tab accepted")
        except BNR:
            pass

        # happy path: selectors + one search, read-only
        report = await run_recon(
            42, PORTAL_BASE, category="26", target_date="30/11/2026",
            bridge_base=BRIDGE_BASE)
        assert report["ok"], report
        assert all(report["checks"].values()), report["checks"]
        assert report["bookable"], report
        assert report["bookable"][0]["time"] == "09:00"
        assert report["empty"] is False
        # the seeded slot was found but nothing was booked/held
        assert not portal.gvcw_bookings
    finally:
        del bridge.tabs[42]
        await ctx.close()


def test_disconnect_and_session_loss(rig):
    portal, browser, bridge, loop = rig
    loop.run_until_complete(_disconnect_flow(portal, browser, bridge))


async def _disconnect_flow(portal: PortalState, browser, bridge: FakeBridge):
    portal.reset()
    ctx, page = await _signed_page(browser, portal, f"{PORTAL_BASE}/appointments/add")
    bridge.tabs[7] = page
    client = BridgeClient(BRIDGE_BASE)
    try:
        bp = await client.page(7, [PORTAL_BASE], allow_writes=True)
        assert await bp.evaluate("document.title") != ""

        # disconnect mid-life: tab vanishes -> next op must raise, not pick another
        del bridge.tabs[7]
        with pytest.raises(BridgeDisconnected):
            await bp.evaluate("document.title")
    finally:
        bridge.tabs.pop(7, None)
        await ctx.close()

    # session loss: page on the login form must stop recon
    ctx2 = await browser.new_context()
    login_page = await ctx2.new_page()
    await login_page.goto(f"{PORTAL_BASE}/gvcw-login")
    bridge.tabs[8] = login_page
    try:
        with pytest.raises(BridgeSessionLost):
            await run_recon(8, PORTAL_BASE, bridge_base=BRIDGE_BASE)
    finally:
        del bridge.tabs[8]
        await ctx2.close()

    # read-only page refuses input
    ctx3, page3 = await _signed_page(browser, portal, f"{PORTAL_BASE}/appointments/add")
    bridge.tabs[9] = page3
    try:
        bp_ro = await client.page(9, [PORTAL_BASE])  # allow_writes=False
        with pytest.raises(BridgeReadOnly):
            await bp_ro.click("#btn-search")
    finally:
        del bridge.tabs[9]
        await ctx3.close()

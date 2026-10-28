"""WebSocket integration test for the LOCAL app's /bridge endpoint.

A fake extension connects over a real socket to localserver.create_app:
token auth, hello handshake, server-initiated command round-trip, and
WsPage tab pinning (a changed popup selection must stop, not follow).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

pytest.importorskip("websockets")
import websockets  # noqa: E402

from app.bridge import HUB, PROTOCOL_VERSION, BridgeError  # noqa: E402
from app.localapp import LocalMonitor  # noqa: E402
from app.localserver import create_app  # noqa: E402
from app.queue import ApplicantQueue  # noqa: E402
from app.ws_page import WsPage  # noqa: E402
from test_hosted_e2e import _serve_in_loop  # noqa: E402

PORTAL = "https://pk-gr-services.gvcworld.eu"


def _ws_url(port, token=None):
    return (f"ws://127.0.0.1:{port}/bridge"
            + (f"?token={token}" if token else ""))


def test_bridge_requires_token(tmp_path):
    loop = asyncio.new_event_loop()

    async def flow():
        queue = ApplicantQueue(tmp_path / "q.db")
        monitor = LocalMonitor(queue, None, PORTAL)
        app = create_app(queue, monitor, HUB, PORTAL, "tok123")
        server, port, task = await _serve_in_loop(app)
        try:
            with pytest.raises(Exception):
                async with websockets.connect(_ws_url(port)):
                    pass
            async with websockets.connect(_ws_url(port, "tok123")) as ws:
                await ws.send(json.dumps({
                    "type": "hello", "protocol": PROTOCOL_VERSION,
                    "version": "test", "tabId": 4242}))
                await asyncio.sleep(0.2)
                assert HUB.connected
        finally:
            server.should_exit = True
            task.cancel()

    loop.run_until_complete(flow())
    loop.close()


def test_command_roundtrip_and_tab_pin(tmp_path):
    loop = asyncio.new_event_loop()

    async def flow():
        queue = ApplicantQueue(tmp_path / "q.db")
        monitor = LocalMonitor(queue, None, PORTAL)
        app = create_app(queue, monitor, HUB, PORTAL, "tok123")
        server, port, task = await _serve_in_loop(app)

        async def fake_extension():
            async with websockets.connect(_ws_url(port, "tok123")) as ws:
                await ws.send(json.dumps({
                    "type": "hello", "protocol": PROTOCOL_VERSION,
                    "version": "test", "tabId": 4242}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("op") == "command":
                        expr = (msg.get("params") or {}).get("expression", "")
                        if "location.href" in expr:
                            val = PORTAL + "/appointments/add"
                        elif "JSON.stringify" in expr:
                            val = '{"login":false,"unauth":false,"challenge":false}'
                        else:
                            val = 42
                        await ws.send(json.dumps({
                            "id": msg["id"], "ok": True,
                            "result": {"result": {"type": "object",
                                                  "value": val}}}))
                    else:
                        await ws.send(json.dumps({
                            "id": msg["id"], "ok": True, "result": {}}))

        ext = asyncio.create_task(fake_extension())
        try:
            for _ in range(60):
                if HUB.connected:
                    break
                await asyncio.sleep(0.05)
            assert HUB.connected
            client = HUB.client()
            assert await client.evaluate("1+1") == 42

            monitor.pair(4242)
            page = WsPage(client, [PORTAL], allow_writes=True,
                          pinned_tab=monitor.tab_id)
            await page.evaluate("document.title")       # pinned tab ok
            # operator selects a DIFFERENT tab in the popup → must stop
            await client.handle_message({"type": "tabChanged", "tabId": 999})
            with pytest.raises(BridgeError):
                await page.evaluate("document.title")
        finally:
            ext.cancel()
            server.should_exit = True
            task.cancel()

    loop.run_until_complete(flow())
    loop.close()

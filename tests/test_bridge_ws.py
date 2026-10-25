"""End-to-end bridge tests over a real WebSocket against a real uvicorn server.

This is the layer that would have caught the `tab_id`/`tabId` mismatch: the
Python fake used by the unit tests never validated wire field names, so only a
test that speaks to the extension over the actual socket proves the protocol.

The server runs **as a task in the test's own event loop** rather than on a
thread. That matters: a request future created on one loop cannot be resolved
from another loop, and a thread-based server made these tests time out for a
reason that does not exist in production (where the crawler and uvicorn share
one loop, as main.py does).
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("websockets")

import websockets  # noqa: E402
import websockets.exceptions as ws_exc  # noqa: E402

from app.bridge import HUB, PROTOCOL_VERSION  # noqa: E402
from app.web import app as fastapi_app  # noqa: E402


async def _start_server():
    """Serve the dashboard app inside the current loop; return (server, task, port)."""
    import uvicorn

    config = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=0, log_level="warning", access_log=False
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    for _ in range(200):
        if instance.started:
            break
        await asyncio.sleep(0.02)
    else:  # pragma: no cover - only on a broken environment
        instance.should_exit = True
        raise RuntimeError("uvicorn did not start")
    port = instance.servers[0].sockets[0].getsockname()[1]
    return instance, task, port


async def _stop_server(instance, task):
    instance.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
        task.cancel()


def _run(scenario):
    async def runner():
        HUB.reset()
        instance, task, port = await _start_server()
        try:
            return await scenario(port)
        finally:
            await _stop_server(instance, task)
            HUB.reset()

    return asyncio.run(runner())


def test_round_trip_over_the_real_socket():
    """hello -> server issues a command -> extension replies -> value returned.

    Proves requirement 1 (camelCase tabId) on the actual wire, plus the handshake
    and request correlation.
    """

    async def scenario(port):
        url = f"ws://127.0.0.1:{port}/bridge"
        async with websockets.connect(url, origin="chrome-extension://testextension") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": PROTOCOL_VERSION,
                        "version": "1.0.0",
                        "tabId": 12,
                    }
                )
            )
            for _ in range(200):
                if HUB.connected:
                    break
                await asyncio.sleep(0.02)
            assert HUB.connected, "server never completed the handshake"
            assert HUB.selected_tab() == 12

            pending = asyncio.create_task(HUB.client().evaluate("document.title"))

            request = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert request["op"] == "command"
            assert request["method"] == "Runtime.evaluate"
            # the regression: the extension reads message.tabId
            assert request.get("tabId") == 12
            assert "tab_id" not in request

            await ws.send(
                json.dumps(
                    {"id": request["id"], "ok": True, "result": {"result": {"value": "GVCW"}}}
                )
            )
            return await asyncio.wait_for(pending, timeout=5)

    assert _run(scenario) == "GVCW"


def test_tab_changed_event_reaches_the_server():
    """Requirement 2: the popup changing tabs must update the backend."""

    async def scenario(port):
        url = f"ws://127.0.0.1:{port}/bridge"
        async with websockets.connect(url, origin="chrome-extension://testextension") as ws:
            await ws.send(
                json.dumps(
                    {"type": "hello", "protocol": PROTOCOL_VERSION, "version": "1.0.0", "tabId": 3}
                )
            )
            for _ in range(200):
                if HUB.connected:
                    break
                await asyncio.sleep(0.02)

            await ws.send(json.dumps({"type": "tabChanged", "tabId": 44}))
            for _ in range(200):
                if HUB.selected_tab() == 44:
                    break
                await asyncio.sleep(0.02)
            return HUB.selected_tab()

    assert _run(scenario) == 44


def test_foreign_origin_is_refused_before_the_handshake():
    """Requirement 8: a web page in the same browser must not reach the portal."""

    async def scenario(port):
        url = f"ws://127.0.0.1:{port}/bridge"
        with pytest.raises((ws_exc.InvalidStatus, ws_exc.InvalidHandshake)):
            async with websockets.connect(url, origin="https://evil.example"):
                pass  # pragma: no cover - must not connect
        return HUB.connected

    assert _run(scenario) is False


def test_shared_token_is_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("AB_BRIDGE_TOKEN", "s3cret")

    async def scenario(port):
        base = f"ws://127.0.0.1:{port}/bridge"
        with pytest.raises((ws_exc.InvalidStatus, ws_exc.InvalidHandshake)):
            async with websockets.connect(base, origin="chrome-extension://e"):
                pass  # pragma: no cover

        async with websockets.connect(
            f"{base}?token=s3cret", origin="chrome-extension://e"
        ) as ws:
            await ws.send(
                json.dumps({"type": "hello", "protocol": PROTOCOL_VERSION, "tabId": None})
            )
            for _ in range(200):
                if HUB.connected:
                    break
                await asyncio.sleep(0.02)
            return HUB.connected

    assert _run(scenario) is True


def test_first_frame_must_be_a_hello():
    """Requirement 6: no commands before the handshake."""

    async def scenario(port):
        url = f"ws://127.0.0.1:{port}/bridge"
        async with websockets.connect(url, origin="chrome-extension://e") as ws:
            await ws.send(json.dumps({"id": "early", "op": "tabs"}))
            with pytest.raises(ws_exc.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=5)
        for _ in range(100):
            if not HUB.socket_count:
                break
            await asyncio.sleep(0.02)
        return HUB.connected

    assert _run(scenario) is False


def test_bad_protocol_closes_the_socket():
    async def scenario(port):
        url = f"ws://127.0.0.1:{port}/bridge"
        async with websockets.connect(url, origin="chrome-extension://e") as ws:
            await ws.send(json.dumps({"type": "hello", "protocol": 99, "tabId": None}))
            with pytest.raises(ws_exc.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=5)
        for _ in range(100):
            if not HUB.socket_count:
                break
            await asyncio.sleep(0.02)
        return HUB.connected

    assert _run(scenario) is False

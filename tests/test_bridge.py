"""Tests for the browser bridge: protocol validation, request correlation,
tab targeting, exception surfacing, and the CDP calls our driver issues.

A fake extension stands in for Chrome, so none of this needs a browser.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from app.bridge import (
    MAX_FRAME_BYTES,
    MAX_PENDING_REQUESTS,
    PROTOCOL_VERSION,
    BridgeClient,
    BridgeError,
    BridgeHub,
    BridgeNotConnected,
    BridgeProtocolError,
    BridgeTimeout,
    parse_frame,
    token_matches,
    validate_origin,
)
from app.site import BookingSelectors, BridgeSiteDriver, SlotNotFound


async def _noop(raw: str) -> None:
    """A send that goes nowhere (the reply never arrives)."""


class FakeExtension:
    """Records what the server sends and replies the way Chrome would."""

    def __init__(self, responder=None):
        self.sent: list[dict] = []
        self.client = BridgeClient(client_id="fake", send_raw=self._send)
        self._responder = responder

    async def _send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        if self._responder is None:
            return
        reply = {"id": message.get("id")}
        reply.update(self._responder(message))
        asyncio.ensure_future(self.client.handle_message(reply))

    def ops(self) -> list[str]:
        return [m.get("op") for m in self.sent]

    def commands(self) -> list[tuple[str, dict]]:
        return [
            (m.get("method"), m.get("params", {}))
            for m in self.sent
            if m.get("op") == "command"
        ]


def responder_ok(value=True, ready_state: str = "complete"):
    """Behave like the page: readyState resolves at once, everything else `value`."""

    def responder(message):
        if message.get("op") != "command":
            return {"ok": True, "result": None}
        expression = str(message.get("params", {}).get("expression", ""))
        if "readyState" in expression:
            return {"ok": True, "result": {"result": {"value": ready_state}}}
        # the filter-application JS reports {found, applied}; emulate success
        if "el.options" in expression:
            return {"ok": True, "result": {"result": {"value": {"found": True, "applied": True}}}}
        return {"ok": True, "result": {"result": {"value": value}}}

    return responder


# ── dependency guard (requirement 9) ────────────────────────────────────

def test_a_websocket_implementation_is_installed():
    """uvicorn needs websockets or wsproto, or /bridge never completes its
    handshake. Plain `uvicorn` does not pull one in by default."""
    available = [name for name in ("websockets", "wsproto") if importlib.util.find_spec(name)]
    assert available, (
        "no WebSocket implementation installed — `pip install websockets` "
        "(it is listed in requirements.txt)"
    )


# ── frame + connection validation (requirement 6) ───────────────────────

def test_frame_parsing_rejects_garbage():
    with pytest.raises(BridgeProtocolError, match="not valid JSON"):
        parse_frame("not json")
    with pytest.raises(BridgeProtocolError, match="JSON object"):
        parse_frame("[1, 2]")
    with pytest.raises(BridgeProtocolError, match="must be text"):
        parse_frame(b"{}")
    with pytest.raises(BridgeProtocolError, match="exceeds"):
        parse_frame("x" * (MAX_FRAME_BYTES + 1))


def test_origin_must_be_the_extension_or_a_local_page():
    assert validate_origin("chrome-extension://abcdef")[0] is True
    assert validate_origin("http://127.0.0.1:8080")[0] is True
    # a malicious web page in the same browser is exactly what this stops
    assert validate_origin("https://evil.example")[0] is False
    assert validate_origin(None)[0] is False


def test_token_check_is_optional_but_strict_when_configured():
    assert token_matches(None, None) is True
    assert token_matches("anything", None) is True
    assert token_matches("right", "right") is True
    assert token_matches("wrong", "right") is False
    assert token_matches(None, "right") is False


def test_hello_protocol_mismatch_is_rejected():
    async def scenario():
        client = BridgeClient(client_id="c", send_raw=_noop)
        await client.handle_message(
            {"type": "hello", "protocol": 99, "version": "1.0.0", "tabId": None}
        )

    with pytest.raises(BridgeProtocolError, match="protocol"):
        asyncio.run(scenario())


def test_hello_sets_compatibility_protocol_and_tab():
    async def scenario():
        client = BridgeClient(client_id="c", send_raw=_noop)
        await client.handle_message(
            {"type": "hello", "protocol": PROTOCOL_VERSION, "version": "1.0.0", "tabId": 42}
        )
        return client.compatible, client.protocol, client.tab_id

    assert asyncio.run(scenario()) == (True, PROTOCOL_VERSION, 42)


def test_reply_shapes_are_validated():
    async def bad_ok():
        client = BridgeClient(client_id="c", send_raw=_noop)
        await client.handle_message({"id": "r1", "ok": "yes"})

    async def bad_id():
        client = BridgeClient(client_id="c", send_raw=_noop)
        await client.handle_message({"ok": True})

    with pytest.raises(BridgeProtocolError, match="reply.ok"):
        asyncio.run(bad_ok())
    with pytest.raises(BridgeProtocolError, match="reply.id"):
        asyncio.run(bad_id())


# ── tab selection reaches the backend (requirement 2) ───────────────────

def test_tab_changed_updates_the_client_and_notifies_observers():
    async def scenario():
        hub = BridgeHub()
        events: list[tuple[str, dict]] = []
        hub.add_observer(lambda name, payload: events.append((name, payload)))
        client = hub.register(BridgeClient(client_id="c", send_raw=_noop))
        await client.handle_message(
            {"type": "hello", "protocol": PROTOCOL_VERSION, "tabId": None}
        )

        await client.handle_message({"type": "tabChanged", "tabId": 77})
        return client.tab_id, hub.selected_tab(), events[-1]

    tab_id, selected, event = asyncio.run(scenario())
    assert tab_id == 77
    assert selected == 77
    assert event[0] == "tab_changed" and event[1]["tab_id"] == 77


def test_tab_closed_clears_the_selection_on_the_server():
    async def scenario():
        hub = BridgeHub()
        client = hub.register(BridgeClient(client_id="c", send_raw=_noop))
        await client.handle_message({"type": "hello", "protocol": PROTOCOL_VERSION, "tabId": 5})
        await client.handle_message({"type": "tabChanged", "tabId": None})
        return client.tab_id, hub.selected_tab()

    assert asyncio.run(scenario()) == (None, None)


def test_tab_changed_rejects_a_non_integer():
    async def scenario():
        client = BridgeClient(client_id="c", send_raw=_noop)
        await client.handle_message({"type": "tabChanged", "tabId": "77"})

    with pytest.raises(BridgeProtocolError, match="tabChanged.tabId"):
        asyncio.run(scenario())


# ── request correlation and lifecycle ───────────────────────────────────

def test_request_response_correlation():
    async def scenario():
        seen = []

        async def send_raw(raw):
            seen.append(json.loads(raw))

        client = BridgeClient(client_id="c", send_raw=send_raw)
        task = asyncio.create_task(client.request("tabs", timeout=2))
        while not seen:
            await asyncio.sleep(0.01)
        await client.handle_message({"id": seen[0]["id"], "ok": True, "result": [{"tabId": 7}]})
        return await task

    assert asyncio.run(scenario()) == [{"tabId": 7}]


def test_error_reply_raises_bridge_error():
    async def scenario():
        seen = []

        async def send_raw(raw):
            seen.append(json.loads(raw))

        client = BridgeClient(client_id="c", send_raw=send_raw)
        task = asyncio.create_task(client.request("command", timeout=2))
        while not seen:
            await asyncio.sleep(0.01)
        await client.handle_message({"id": seen[0]["id"], "ok": False, "error": "Tab not attached"})
        return await task

    with pytest.raises(BridgeError, match="Tab not attached"):
        asyncio.run(scenario())


def test_timeout_is_reported_not_hung():
    async def scenario():
        client = BridgeClient(client_id="c", send_raw=_noop)
        return await client.request("tabs", timeout=0.05)

    with pytest.raises(BridgeTimeout):
        asyncio.run(scenario())


def test_disconnect_fails_in_flight_requests():
    async def scenario():
        client = BridgeClient(client_id="c", send_raw=_noop)
        task = asyncio.create_task(client.request("tabs", timeout=5))
        await asyncio.sleep(0.01)
        client.close("extension went away")
        return await task

    with pytest.raises(BridgeNotConnected):
        asyncio.run(scenario())


def test_backpressure_refuses_work_beyond_the_limit():
    async def scenario():
        client = BridgeClient(client_id="c", send_raw=_noop)
        tasks = [
            asyncio.create_task(client.request("tabs", timeout=5))
            for _ in range(MAX_PENDING_REQUESTS)
        ]
        await asyncio.sleep(0.05)
        try:
            with pytest.raises(BridgeError, match="too many requests"):
                await client.request("tabs", timeout=0.1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_hub_reports_when_nothing_is_attached():
    hub = BridgeHub()
    assert hub.connected is False
    assert hub.selected_tab() is None
    with pytest.raises(BridgeNotConnected, match="no browser extension connected"):
        hub.client()


def test_hub_tracks_register_unregister_and_snapshot():
    async def scenario():
        hub = BridgeHub()
        events: list[str] = []
        hub.add_observer(lambda name, payload: events.append(name))
        client = hub.register(BridgeClient(client_id="c", send_raw=_noop))
        await client.handle_message(
            {"type": "hello", "protocol": PROTOCOL_VERSION, "version": "1.0.0", "tabId": 3}
        )
        snapshot = hub.snapshot()
        hub.unregister(client)
        return hub.connected, hub.client_count, snapshot[0]["tab_id"], events

    connected, count, tab_id, events = asyncio.run(scenario())
    assert (connected, count, tab_id) == (False, 0, 3)
    assert events == ["connected", "disconnected"]


# ── exceptionDetails (requirement 4) ────────────────────────────────────

def test_evaluate_surfaces_exception_details_as_an_error():
    """Runtime.evaluate reports page errors in exceptionDetails rather than
    failing, so ignoring them made a throwing selector look like `undefined`."""

    def responder(message):
        return {
            "ok": True,
            "result": {
                "result": {"type": "object"},
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {"description": "TypeError: document.querySelector is null"},
                },
            },
        }

    fake = FakeExtension(responder=responder)
    fake.client.tab_id = 1
    with pytest.raises(BridgeError, match="TypeError: document.querySelector is null"):
        asyncio.run(fake.client.evaluate("document.querySelector(null).click()"))


def test_evaluate_rejects_a_reply_without_a_result_object():
    def responder(message):
        return {"ok": True, "result": {}}

    fake = FakeExtension(responder=responder)
    fake.client.tab_id = 1
    with pytest.raises(BridgeProtocolError, match="no result object"):
        asyncio.run(fake.client.evaluate("1"))


def test_command_without_a_selected_tab_is_a_clear_error():
    client = BridgeClient(client_id="c", send_raw=_noop)
    with pytest.raises(BridgeNotConnected, match="no tab selected"):
        asyncio.run(client.command("Runtime.evaluate", {"expression": "1"}))


# ── driver behaviour (requirement 7) ────────────────────────────────────

def _selectors() -> BookingSelectors:
    return BookingSelectors(
        vac_select="#vac",
        category_select="#cat",
        slot_row=".slot-row",
        reserve_button="button.reserve",
        applicant_fields={"passport": "#passport"},
        submit_button="#submit",
        confirmation_marker="#confirmation",
    )


def _driver(responder):
    fake = FakeExtension(responder=responder)
    fake.client.tab_id = 5
    return BridgeSiteDriver(fake.client, _selectors()), fake


def test_driver_follows_the_operators_tab_and_honours_a_pin():
    async def scenario():
        fake = FakeExtension(responder=responder_ok())
        fake.client.tab_id = 5
        following = BridgeSiteDriver(fake.client, _selectors())

        await following.client.evaluate("1")
        first = [m["tabId"] for m in fake.sent if m.get("op") == "command"][-1]

        # the operator picks a different tab in the popup
        await fake.client.handle_message({"type": "tabChanged", "tabId": 9})
        await following.client.evaluate("1")
        second = [m["tabId"] for m in fake.sent if m.get("op") == "command"][-1]

        pinned = BridgeSiteDriver(fake.client, _selectors(), tab_id=11)
        await pinned.client.evaluate("1", pinned.tab_id)
        third = [m["tabId"] for m in fake.sent if m.get("op") == "command"][-1]
        return first, second, third

    assert asyncio.run(scenario()) == (5, 9, 11)


def test_locate_reports_true_when_the_row_exists():
    driver, fake = _driver(responder_ok(value=True))
    found = asyncio.run(
        driver.locate(
            {"date": "2026-12-01", "time": "09:00", "location": "lahore",
             "category": "national_d_employment"}
        )
    )
    assert found is True
    js = " ".join(str(p) for _, p in fake.commands())
    assert "#vac" in js and "#cat" in js


def test_reserve_dispatches_real_mouse_events():
    driver, fake = _driver(responder_ok(value={"x": 120, "y": 240}))
    asyncio.run(
        driver.reserve(
            {"date": "2026-12-01", "time": "09:00", "location": "lahore",
             "category": "national_d_employment"}
        )
    )

    kinds = [params.get("type") for method, params in fake.commands()
             if method == "Input.dispatchMouseEvent"]
    assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert any("button.reserve" in str(params) for _, params in fake.commands())


def test_reserve_raises_slot_gone_when_the_row_is_missing():
    driver, _ = _driver(responder_ok(value=None))
    with pytest.raises(SlotNotFound):
        asyncio.run(
            driver.reserve(
                {"date": "2026-12-01", "time": "09:00", "location": "lahore",
                 "category": "national_d_employment"}
            )
        )


def test_driver_refuses_to_act_without_selectors():
    fake = FakeExtension(responder=responder_ok())
    fake.client.tab_id = 1
    driver = BridgeSiteDriver(fake.client, BookingSelectors())
    with pytest.raises(Exception, match="selectors are not configured"):
        asyncio.run(
            driver.locate({"date": "2026-12-01", "location": "lahore",
                           "category": "national_d_employment"})
        )


def test_wait_for_waf_detects_the_block_shell():
    def responder(message):
        if message.get("op") == "command":
            return {"ok": True, "result": {"result": {"value": "Access denied — Error 17"}}}
        return {"ok": True, "result": {}}

    driver, _ = _driver(responder)
    with pytest.raises(Exception, match="block shell"):
        asyncio.run(driver.wait_for_waf())


def test_screenshot_decodes_base64(tmp_path: Path):
    import base64

    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()

    def responder(message):
        if message.get("method") == "Page.captureScreenshot":
            return {"ok": True, "result": {"data": png}}
        return {"ok": True, "result": {}}

    driver, _ = _driver(responder)
    target = tmp_path / "evidence.png"
    asyncio.run(driver.screenshot(target))
    assert target.read_bytes().startswith(b"\x89PNG")


# ── wire protocol (requirement 1) ───────────────────────────────────────

def test_command_sends_the_camel_case_tab_id_the_extension_reads():
    """Regression: the extension reads `message.tabId`, so the wire field must be
    `tabId`. Sending `tab_id` silently left every command with no target tab."""

    async def scenario():
        seen = []

        async def send_raw(raw):
            seen.append(json.loads(raw))

        client = BridgeClient(client_id="c", send_raw=send_raw)
        client.tab_id = 9
        task = asyncio.create_task(client.command("Runtime.evaluate", {"expression": "1"}))
        while not seen:
            await asyncio.sleep(0.01)
        await client.handle_message({"id": seen[0]["id"], "ok": True, "result": {}})
        await task
        return seen[0]

    payload = asyncio.run(scenario())
    assert payload["tabId"] == 9
    assert "tab_id" not in payload
    assert payload["method"] == "Runtime.evaluate"


def test_network_sends_the_camel_case_tab_id():
    async def scenario():
        seen = []

        async def send_raw(raw):
            seen.append(json.loads(raw))

        client = BridgeClient(client_id="c", send_raw=send_raw)
        client.tab_id = 4
        task = asyncio.create_task(client.network(limit=10))
        while not seen:
            await asyncio.sleep(0.01)
        await client.handle_message({"id": seen[0]["id"], "ok": True, "result": {}})
        await task
        return seen[0]

    payload = asyncio.run(scenario())
    assert payload["tabId"] == 4
    assert "tab_id" not in payload

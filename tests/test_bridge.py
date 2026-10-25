"""Tests for the browser bridge: request correlation, timeouts, and the
CDP calls our driver issues.

A fake extension stands in for Chrome, so none of this needs a browser.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.bridge import (
    BridgeClient,
    BridgeError,
    BridgeHub,
    BridgeNotConnected,
    BridgeTimeout,
)
from app.site import BookingSelectors, BridgeSiteDriver, SlotNotFound


class FakeExtension:
    """Stands in for the extension: records requests, replies like Chrome."""

    def __init__(self, responder=None):
        self.sent: list[dict] = []
        self.client = BridgeClient(client_id="fake", send_raw=self._send)
        self._responder = responder
        self._loop = None

    async def _send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        if self._responder is None:
            return
        # reply out-of-band, the way a WebSocket reader would
        loop = asyncio.get_running_loop()
        loop.call_soon(
            lambda: asyncio.ensure_future(
                self.client.handle_message(
                    {"id": message.get("id"), **self._responder(message)}
                )
            )
        )

    def ops(self) -> list[str]:
        return [m.get("op") for m in self.sent]

    def commands(self) -> list[tuple[str, dict]]:
        return [(m.get("method"), m.get("params", {})) for m in self.sent if m.get("op") == "command"]


def idle_responder(message):
    return {"ok": True, "result": {"ok": True, "result": {"value": None}}}


def test_request_response_correlation():
    """A reply resolves the matching request by id."""

    async def scenario():
        seen = []

        async def send_raw(raw):
            payload = json.loads(raw)
            seen.append(payload)

        client = BridgeClient(client_id="c", send_raw=send_raw)
        task = asyncio.create_task(client.request("tabs", timeout=2))

        # wait until the request was written, then answer it
        while not seen:
            await asyncio.sleep(0.01)
        await client.handle_message({"id": seen[0]["id"], "ok": True, "result": [{"tabId": 7}]})
        return await task

    assert asyncio.run(scenario()) == [{"tabId": 7}]


def test_hello_records_the_selected_tab():
    async def scenario():
        client = BridgeClient(client_id="c", send_raw=lambda raw: asyncio.sleep(0))
        await client.handle_message(
            {"type": "hello", "version": "1.0.0", "protocol": 1, "tabId": 42}
        )
        return client.tab_id, client.info.get("version")

    tab_id, version = asyncio.run(scenario())
    assert tab_id == 42
    assert version == "1.0.0"


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
        client = BridgeClient(client_id="c", send_raw=lambda raw: asyncio.sleep(0))
        return await client.request("tabs", timeout=0.05)

    with pytest.raises(BridgeTimeout):
        asyncio.run(scenario())


def test_disconnect_fails_in_flight_requests():
    """Losing the extension must fail the booking call, never hang it."""

    async def scenario():
        client = BridgeClient(client_id="c", send_raw=lambda raw: asyncio.sleep(0))
        task = asyncio.create_task(client.request("tabs", timeout=5))
        await asyncio.sleep(0.01)
        client.close("extension went away")
        return await task

    with pytest.raises(BridgeNotConnected):
        asyncio.run(scenario())


def test_hub_reports_when_nothing_is_attached():
    hub = BridgeHub()
    assert hub.connected is False
    with pytest.raises(BridgeNotConnected, match="no browser extension connected"):
        hub.client()


def test_hub_tracks_register_and_unregister():
    async def scenario():
        hub = BridgeHub()
        client = hub.register(BridgeClient(client_id="c", send_raw=lambda raw: asyncio.sleep(0)))
        assert hub.connected is True and hub.client_count == 1
        hub.unregister(client)
        return hub.connected, hub.client_count

    assert asyncio.run(scenario()) == (False, 0)


# ── driver behaviour ─────────────────────────────────────────────────────

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


def test_locate_reports_true_when_the_row_exists():
    def responder(message):
        if message.get("op") == "command":
            return {"ok": True, "result": {"result": {"value": True}}}
        return {"ok": True, "result": None}

    driver, fake = _driver(responder)
    found = asyncio.run(driver.locate({"date": "2026-12-01", "time": "09:00",
                                      "location": "lahore", "category": "national_d_employment"}))
    assert found is True
    # it must set the VAC and category selects before deciding
    js = " ".join(str(p) for _, p in fake.commands())
    assert "#vac" in js and "#cat" in js


def test_reserve_dispatches_real_mouse_events():
    """Trusted mouse events, not a JS .click()."""

    def responder(message):
        if message.get("op") != "command":
            return {"ok": True, "result": None}
        if message.get("method") == "Input.dispatchMouseEvent":
            return {"ok": True, "result": {}}
        if message.get("method") == "Runtime.evaluate":
            return {"ok": True, "result": {"result": {"value": {"x": 120, "y": 240}}}}
        return {"ok": True, "result": {}}

    driver, fake = _driver(responder)
    asyncio.run(driver.reserve({"date": "2026-12-01", "time": "09:00",
                                "location": "lahore", "category": "national_d_employment"}))

    kinds = [params.get("type") for method, params in fake.commands()
             if method == "Input.dispatchMouseEvent"]
    assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"]
    # and the click landed on the button inside the matched row
    assert any("button.reserve" in str(params) for _, params in fake.commands())


def test_reserve_raises_slot_gone_when_the_row_is_missing():
    def responder(message):
        if message.get("method") == "Runtime.evaluate":
            return {"ok": True, "result": {"result": {"value": None}}}
        return {"ok": True, "result": {}}

    driver, _ = _driver(responder)
    with pytest.raises(SlotNotFound):
        asyncio.run(driver.reserve({"date": "2026-12-01", "time": "09:00",
                                    "location": "lahore", "category": "national_d_employment"}))


def test_driver_refuses_to_act_without_selectors():
    def responder(message):
        return {"ok": True, "result": {"result": {"value": True}}}

    client = BridgeClient(client_id="c", send_raw=lambda raw: asyncio.sleep(0))
    client.tab_id = 1
    driver = BridgeSiteDriver(client, BookingSelectors())  # nothing filled in
    with pytest.raises(Exception, match="selectors are not configured"):
        asyncio.run(driver.locate({"date": "2026-12-01", "location": "lahore",
                                   "category": "national_d_employment"}))


def test_wait_for_waf_detects_the_block_shell():
    def responder(message):
        if message.get("method") == "Runtime.evaluate":
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

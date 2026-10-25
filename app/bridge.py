"""Loopback bridge between the crawler and our browser extension.

The extension (see `extension/`) dials `ws://127.0.0.1:<port>/bridge` and
executes Chrome DevTools Protocol commands on a tab the operator picked. That is
how the agent can drive the browser the consultant is *already signed into*,
instead of a throwaway profile that has to sign in again past the reCAPTCHA.

Why this shape rather than the native-messaging approach most projects use:
the crawler already runs an HTTP server for its dashboard, so the extension can
dial out to it. One process, no Node runtime in the product, no registry keys
and no per-OS installer.

This module is deliberately transport-agnostic: the WebSocket lives in web.py,
and everything here can be driven directly from tests via a fake `send_raw`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT = 30.0


class BridgeError(RuntimeError):
    """Anything the extension reported as a failure."""


class BridgeNotConnected(BridgeError):
    """No extension is attached (or it went away mid-request)."""


class BridgeTimeout(BridgeError):
    """The extension did not answer in time."""


@dataclass
class BridgeClient:
    """One connected extension, with request/response correlation."""

    client_id: str
    send_raw: Callable[[str], Awaitable[None]]
    info: dict[str, Any] = field(default_factory=dict)
    tab_id: int | None = None
    _pending: dict[str, asyncio.Future] = field(default_factory=dict, repr=False)
    _closed: bool = False

    # ── inbound ──────────────────────────────────────────────────────────

    async def handle_message(self, message: dict) -> None:
        """Route one message received from the extension."""
        kind = message.get("type")
        if kind == "hello":
            self.info = message
            if message.get("tabId") is not None:
                self.tab_id = message["tabId"]
            logger.info(
                "bridge: extension connected (v%s, protocol=%s)",
                message.get("version"),
                message.get("protocol"),
            )
            return
        if kind in ("ping", "pong"):
            return

        request_id = message.get("id")
        future = self._pending.pop(request_id, None) if request_id else None
        if future is None or future.done():
            return
        if message.get("ok"):
            future.set_result(message.get("result"))
        else:
            future.set_exception(BridgeError(message.get("error") or "extension error"))

    # ── outbound ─────────────────────────────────────────────────────────

    async def request(self, op: str, timeout: float = DEFAULT_TIMEOUT, **fields: Any) -> Any:
        if self._closed:
            raise BridgeNotConnected("extension disconnected")
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"id": request_id, "op": op, **fields}
        try:
            await self.send_raw(json.dumps(payload))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BridgeTimeout(f"{op} timed out after {timeout}s") from exc
        finally:
            self._pending.pop(request_id, None)

    def close(self, reason: str = "") -> None:
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BridgeNotConnected(reason or "extension disconnected"))
        self._pending.clear()

    # ── helpers used by the drivers ──────────────────────────────────────

    async def status(self) -> dict:
        return await self.request("status")

    async def tabs(self, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
        return await self.request("tabs", timeout=timeout)

    async def command(
        self,
        method: str,
        params: dict | None = None,
        tab_id: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict:
        target = tab_id if tab_id is not None else self.tab_id
        if target is None:
            raise BridgeNotConnected("no tab selected in the extension popup")
        return await self.request(
            "command", tab_id=target, method=method, params=params or {}, timeout=timeout
        )

    async def evaluate(self, expression: str, tab_id: int | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Evaluate JS in the tab and return its value."""
        result = await self.command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            tab_id,
            timeout,
        )
        return (result or {}).get("result", {}).get("value")

    async def network(self, tab_id: int | None = None, **options: Any) -> dict:
        target = tab_id if tab_id is not None else self.tab_id
        return await self.request("network", tab_id=target, **options)

    async def screenshot_b64(self, tab_id: int | None = None) -> str | None:
        result = await self.command("Page.captureScreenshot", {"format": "png"}, tab_id)
        return (result or {}).get("data")


class BridgeHub:
    """Registry of connected extensions. Picks one client to serve requests."""

    def __init__(self) -> None:
        self._clients: list[BridgeClient] = []

    def register(self, client: BridgeClient) -> BridgeClient:
        self._clients.append(client)
        return client

    def unregister(self, client: BridgeClient) -> None:
        if client in self._clients:
            self._clients.remove(client)
        client.close()

    @property
    def connected(self) -> bool:
        return bool(self._clients)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def client(self) -> BridgeClient:
        if not self._clients:
            raise BridgeNotConnected(
                "no browser extension connected — open the Appointment Booker "
                "extension and press Connect"
            )
        return self._clients[-1]

    def reset(self) -> None:
        for client in list(self._clients):
            client.close("hub reset")
        self._clients.clear()


# A process-wide hub: the dashboard server registers extensions into it and the
# crawler workers read from it.
HUB = BridgeHub()

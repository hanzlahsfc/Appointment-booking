"""Loopback bridge between the crawler and our browser extension.

The extension (see `extension/`) dials `ws://127.0.0.1:<port>/bridge` and
executes Chrome DevTools Protocol commands on a tab the operator picked. That is
how the agent can drive the browser the consultant is *already signed into*,
instead of a throwaway profile that has to sign in again past the reCAPTCHA.

Why this shape rather than the native-messaging approach most projects use:
the crawler already runs an HTTP server for its dashboard, so the extension can
dial out to it. One process, no Node runtime in the product, no registry keys
and no per-OS installer.

Wire protocol (JSON text frames, camelCase because one side is JavaScript):

    extension -> server   {"type":"hello","protocol":1,"version":"1.0.0","tabId":null}
    extension -> server   {"type":"tabChanged","tabId":123}
    extension -> server   {"type":"pong"}
    server -> extension   {"id":"<hex>","op":"command","tabId":123,"method":"...","params":{}}
    extension -> server   {"id":"<hex>","ok":true,"result":{...}}

Trust boundary (see also web.py): the socket is loopback-only and the Origin
must be `chrome-extension://…`, which is what stops a *web page* in the same
browser from driving the portal through this bridge. Origin is header-supplied
and therefore not authentication, so setting `AB_BRIDGE_TOKEN` adds a shared
secret the extension must present. Neither defends against a hostile process
already running as the same user — such a process can control the browser by
other means.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT = 30.0

#: Text frames larger than this are rejected rather than buffered.
MAX_FRAME_BYTES = 8 * 1024 * 1024
#: Backpressure guard: refuse new work rather than queueing without bound.
MAX_PENDING_REQUESTS = 64


class BridgeError(RuntimeError):
    """Anything the extension reported as a failure."""


class TabInUse(BridgeError):
    """The requested tab is already being driven by another worker."""


class BridgeNotConnected(BridgeError):
    """No extension is attached (or it went away mid-request)."""


class BridgeTimeout(BridgeError):
    """The extension did not answer in time."""


class BridgeProtocolError(BridgeError):
    """A frame or reply did not match the wire protocol."""


# ── frame + connection validation ───────────────────────────────────────────


def parse_frame(raw: Any) -> dict[str, Any]:
    """Decode one inbound frame, rejecting anything off-protocol."""
    if not isinstance(raw, str):
        raise BridgeProtocolError("frame must be text")
    if len(raw.encode("utf-8", "ignore")) > MAX_FRAME_BYTES:
        raise BridgeProtocolError(f"frame exceeds {MAX_FRAME_BYTES} bytes")
    try:
        message = json.loads(raw)
    except ValueError as exc:
        raise BridgeProtocolError("frame is not valid JSON") from exc
    if not isinstance(message, dict):
        raise BridgeProtocolError("frame must be a JSON object")
    return message


def validate_origin(origin: str | None) -> tuple[bool, str]:
    """Only our extension (or a local page) may open the bridge.

    A WebSocket request is not subject to CORS, so without this check any web
    page in the same browser could connect to loopback and drive the portal.
    """
    if not origin:
        return False, "missing Origin header"
    if origin.startswith("chrome-extension://"):
        return True, "extension"
    if origin in ("http://127.0.0.1", "http://localhost"):
        return True, "local page"
    if origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
        return True, "local page"
    return False, f"unexpected Origin {origin!r}"


def token_matches(provided: str | None, expected: str | None) -> bool:
    """Shared-secret check; disabled (always true) when no token is configured."""
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _describe_exception(details: dict[str, Any]) -> str:
    """Best human-readable text from Runtime.evaluate exceptionDetails."""
    exception = details.get("exception")
    if isinstance(exception, dict):
        for key in ("description", "value"):
            value = exception.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().splitlines()[0][:400]
    text = details.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()[:400]
    return "unknown JavaScript exception"


# ── client ──────────────────────────────────────────────────────────────────


@dataclass
class BridgeClient:
    """One connected extension, with request/response correlation."""

    client_id: str
    send_raw: Callable[[str], Awaitable[None]]
    info: dict[str, Any] = field(default_factory=dict)
    tab_id: int | None = None
    protocol: int | None = None
    compatible: bool | None = None
    handshaken: bool = False
    on_tab_change: Callable[["BridgeClient", int | None], None] | None = None
    _pending: dict[str, asyncio.Future] = field(default_factory=dict, repr=False)
    _closed: bool = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def in_flight(self) -> int:
        return len(self._pending)

    # ── inbound ──────────────────────────────────────────────────────────

    async def handle_message(self, message: Any) -> None:
        """Route one decoded message received from the extension."""
        if not isinstance(message, dict):
            raise BridgeProtocolError("message must be a JSON object")

        kind = message.get("type")
        if kind is not None:
            if not isinstance(kind, str):
                raise BridgeProtocolError("message.type must be a string")
            self._handle_event(kind, message)
            return
        self._handle_reply(message)

    def _handle_event(self, kind: str, message: dict[str, Any]) -> None:
        if kind == "hello":
            protocol = message.get("protocol")
            if protocol != PROTOCOL_VERSION:
                self.compatible = False
                self.protocol = protocol if isinstance(protocol, int) else None
                raise BridgeProtocolError(
                    f"extension speaks protocol {protocol!r}, server speaks {PROTOCOL_VERSION}"
                )
            self.compatible = True
            self.protocol = protocol
            self.handshaken = True
            self.info = message
            tab_id = message.get("tabId")
            if tab_id is not None and not isinstance(tab_id, int):
                raise BridgeProtocolError("hello.tabId must be an integer or null")
            self.tab_id = tab_id
            logger.info(
                "bridge: extension connected (version=%s, protocol=%s, tab=%s)",
                message.get("version"),
                protocol,
                tab_id,
            )
            return

        if kind == "tabChanged":
            tab_id = message.get("tabId")
            if tab_id is not None and not isinstance(tab_id, int):
                raise BridgeProtocolError("tabChanged.tabId must be an integer or null")
            previous, self.tab_id = self.tab_id, tab_id
            if previous != tab_id:
                logger.info("bridge: selected tab %s -> %s", previous, tab_id)
            if self.on_tab_change is not None:
                self.on_tab_change(self, tab_id)
            return

        if kind in ("ping", "pong"):
            return

        logger.debug("bridge: ignoring unknown event %r", kind)

    def _handle_reply(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise BridgeProtocolError("reply.id must be a non-empty string")
        ok = message.get("ok")
        if not isinstance(ok, bool):
            raise BridgeProtocolError("reply.ok must be a boolean")

        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            # Late reply to a timed-out request: nothing to resolve.
            logger.debug("bridge: reply for unknown request id %s", request_id)
            return

        if ok:
            future.set_result(message.get("result"))
        else:
            error = message.get("error")
            if not isinstance(error, str) or not error:
                error = "extension reported a failure without a message"
            future.set_exception(BridgeError(error))

    # ── outbound ─────────────────────────────────────────────────────────

    async def request(self, op: str, timeout: float = DEFAULT_TIMEOUT, **fields: Any) -> Any:
        if self._closed:
            raise BridgeNotConnected("extension disconnected")
        if not isinstance(op, str) or not op:
            raise BridgeProtocolError("op must be a non-empty string")
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise BridgeError(
                f"too many requests in flight ({len(self._pending)}); the extension is not keeping up"
            )

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

    def current_tab(self) -> int:
        """The operator's selected tab, resolved at call time."""
        if self.tab_id is None:
            raise BridgeNotConnected(
                "no tab selected — choose one in the Appointment Booker extension popup"
            )
        return self.tab_id

    async def status(self) -> dict:
        return await self.request("status")

    async def tabs(self, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
        result = await self.request("tabs", timeout=timeout)
        if not isinstance(result, list):
            raise BridgeProtocolError("tabs reply was not a list")
        return result

    async def command(
        self,
        method: str,
        params: dict | None = None,
        tab_id: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict:
        if not isinstance(method, str) or not method:
            raise BridgeProtocolError("method must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise BridgeProtocolError("params must be an object")
        target = tab_id if tab_id is not None else self.current_tab()
        # The extension is JavaScript: the wire field is camelCase `tabId`.
        reply = await self.request(
            "command", tabId=target, method=method, params=params or {}, timeout=timeout
        )
        if not isinstance(reply, dict):
            raise BridgeProtocolError("command reply was not an object")
        return reply

    async def evaluate(
        self, expression: str, tab_id: int | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> Any:
        """Evaluate JS in the tab, raising if the page threw.

        `Runtime.evaluate` returns `exceptionDetails` rather than failing the
        command, so without this check a broken selector or a page error would
        look like a successful evaluation returning `undefined`.
        """
        if not isinstance(expression, str) or not expression.strip():
            raise BridgeProtocolError("expression must be a non-empty string")
        reply = await self.command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            tab_id,
            timeout,
        )
        details = reply.get("exceptionDetails")
        if details:
            if not isinstance(details, dict):
                raise BridgeError("Runtime.evaluate returned malformed exceptionDetails")
            raise BridgeError(f"JavaScript exception: {_describe_exception(details)}")
        inner = reply.get("result")
        if not isinstance(inner, dict):
            raise BridgeProtocolError("Runtime.evaluate reply had no result object")
        return inner.get("value")

    async def network(self, tab_id: int | None = None, **options: Any) -> dict:
        target = tab_id if tab_id is not None else self.current_tab()
        reply = await self.request("network", tabId=target, **options)
        if not isinstance(reply, dict):
            raise BridgeProtocolError("network reply was not an object")
        return reply

    async def screenshot_b64(self, tab_id: int | None = None) -> str | None:
        reply = await self.command("Page.captureScreenshot", {"format": "png"}, tab_id)
        data = reply.get("data")
        return data if isinstance(data, str) else None


# ── hub ─────────────────────────────────────────────────────────────────────


class BridgeHub:
    """Registry of connected extensions. Picks one client to serve requests."""

    def __init__(self) -> None:
        self._clients: list[BridgeClient] = []
        self._observers: list[Callable[[str, dict[str, Any]], None]] = []
        self._tab_owners: dict[int, str] = {}

    def add_observer(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Notify on connect/disconnect/tab changes (used by the orchestrator)."""
        self._observers.append(callback)

    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        for callback in list(self._observers):
            try:
                callback(event, payload)
            except Exception:
                logger.exception("bridge: observer failed for %s", event)

    def register(self, client: BridgeClient) -> BridgeClient:
        client.on_tab_change = lambda c, tab_id: self._notify(
            "tab_changed", {"client_id": c.client_id, "tab_id": tab_id}
        )
        self._clients.append(client)
        self._notify("connected", {"client_id": client.client_id})
        return client

    def unregister(self, client: BridgeClient) -> None:
        if client in self._clients:
            self._clients.remove(client)
        client.close()
        self._notify("disconnected", {"client_id": client.client_id})

    @property
    def connected(self) -> bool:
        """True only when an extension has completed the handshake.

        Sockets that have connected but not yet said hello are not usable: a
        worker that grabbed one would get "no tab selected" instead of work.
        """
        return any(c.handshaken for c in self._clients)

    @property
    def client_count(self) -> int:
        """Open sockets, handshaken or not (used for logging)."""
        return len(self._clients)

    @property
    def socket_count(self) -> int:
        return len(self._clients)

    def client(self) -> BridgeClient:
        for client in reversed(self._clients):
            if client.handshaken:
                return client
        if self._clients:
            raise BridgeNotConnected(
                "the extension is connected but has not completed the handshake yet"
            )
        raise BridgeNotConnected(
            "no browser extension connected — open the Appointment Booker "
            "extension and press Connect"
        )

    def selected_tab(self) -> int | None:
        for client in reversed(self._clients):
            if client.handshaken:
                return client.tab_id
        return None

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "client_id": c.client_id,
                "protocol": c.protocol,
                "compatible": c.compatible,
                "handshaken": c.handshaken,
                "tab_id": c.tab_id,
                "version": c.info.get("version"),
                "in_flight": c.in_flight,
            }
            for c in self._clients
        ]

    # ── tab claiming ──────────────────────────────────────────────────────

    def claim_tab(self, tab_id: int, owner: str) -> None:
        """Pin `tab_id` to `owner` for the duration of an operation.

        Two workers must never drive the same tab at once: a second navigation
        would yank the page out from under an in-flight booking (or a human
        finishing a form). Held for the whole poll, so a worker in the human
        hand-off hold keeps its tab reserved until restart.
        """
        current = self._tab_owners.get(tab_id)
        if current is not None and current != owner:
            raise TabInUse(
                f"tab {tab_id} is already being driven by {current!r} — "
                "pick a different tab in the extension popup or set a per-profile tab_id"
            )
        self._tab_owners[tab_id] = owner

    def release_tab(self, tab_id: int, owner: str) -> None:
        if self._tab_owners.get(tab_id) == owner:
            self._tab_owners.pop(tab_id, None)

    def tab_owner(self, tab_id: int) -> str | None:
        return self._tab_owners.get(tab_id)

    def reset(self) -> None:
        for client in list(self._clients):
            client.close("hub reset")
        self._clients.clear()
        self._tab_owners.clear()


# A process-wide hub: the dashboard server registers extensions into it and the
# crawler workers read from it.
HUB = BridgeHub()

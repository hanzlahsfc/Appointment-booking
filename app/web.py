"""FastAPI dashboard — serves the HTML page, /api/status JSON, and the
WebSocket endpoint our browser extension dials into.

Bridge hardening lives here because it is transport-level: Origin allow-list,
optional shared token, frame size, and the hello handshake all happen before any
command reaches a client. See app/bridge.py for the trust-boundary rationale.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from .bridge import (
    HUB,
    MAX_FRAME_BYTES,
    BridgeClient,
    BridgeProtocolError,
    parse_frame,
    token_matches,
    validate_origin,
)

if TYPE_CHECKING:
    from .dashboard import DashboardState

logger = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"

#: When set, the extension must present this value as ?token=… on the bridge URL.
BRIDGE_TOKEN_ENV = "AB_BRIDGE_TOKEN"

app = FastAPI(title="GVCW Slot Monitor", docs_url=None, redoc_url=None)

_dashboard: "DashboardState | None" = None
_config: dict = {}


def bind(dashboard: "DashboardState", config: dict) -> None:
    global _dashboard, _config
    _dashboard = dashboard
    _config = config


def _bridge_token() -> str | None:
    value = (os.environ.get(BRIDGE_TOKEN_ENV) or "").strip()
    return value or None


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/api/status")
async def status():
    if _dashboard is None:
        return JSONResponse({"error": "not ready"}, status_code=503)
    return JSONResponse(_dashboard.snapshot(app_cfg=_config))


@app.get("/api/bridge")
async def bridge_status():
    """Bridge state for the dashboard/CLI."""
    return JSONResponse(
        {
            "connected": HUB.connected,
            "clients": HUB.client_count,
            "sockets": HUB.socket_count,
            "selectedTab": HUB.selected_tab(),
            "extensions": HUB.snapshot(),
            "tokenRequired": _bridge_token() is not None,
        }
    )


@app.get("/api/bridge/network")
async def bridge_network(tab: int, limit: int = 100, urlIncludes: str | None = None):
    """Dump the network traffic the extension captured on a tab.

    Bridge-based recon: once the debugger has attached to the operator's tab
    (any driven poll does this), every request/response — including POST
    bodies — lands in the extension's per-tab log. Clicking through the slot
    picker while the crawler runs records the availability endpoints, which is
    how recon/booking-selectors.json gets filled without Playwright or CDP.
    """
    try:
        client = HUB.client()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    try:
        entries = await client.network(
            tab_id=tab, limit=limit, urlIncludes=urlIncludes
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"tab": tab, "entries": entries})


@app.websocket("/bridge")
async def bridge_socket(websocket: WebSocket) -> None:
    """The browser extension connects here and answers CDP commands.

    Rejections happen **before** accept so the client gets a clean handshake
    failure rather than a socket that dies later. Requests are correlated by id;
    closing the socket fails any in-flight request instead of hanging a booking.
    """
    origin = websocket.headers.get("origin")
    allowed, why = validate_origin(origin)
    if not allowed:
        logger.warning("bridge: refused connection: %s", why)
        await websocket.close(code=1008)  # policy violation
        return

    if not token_matches(websocket.query_params.get("token"), _bridge_token()):
        logger.warning("bridge: refused connection: bad or missing token")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    client = HUB.register(
        BridgeClient(client_id=uuid.uuid4().hex, send_raw=websocket.send_text)
    )
    logger.info("bridge: extension socket opened via %s (%d attached)", why, HUB.client_count)

    greeted = False
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8", "ignore")) > MAX_FRAME_BYTES:
                raise BridgeProtocolError(f"frame exceeds {MAX_FRAME_BYTES} bytes")
            message = parse_frame(raw)

            if not greeted:
                if message.get("type") != "hello":
                    raise BridgeProtocolError("first frame must be a hello")
                greeted = True

            await client.handle_message(message)
    except WebSocketDisconnect:
        pass
    except BridgeProtocolError as exc:
        logger.warning("bridge: protocol error: %s", exc)
        try:
            await websocket.close(code=1002)  # protocol error
        except Exception:
            pass
    except Exception:
        logger.exception("bridge: socket failed")
    finally:
        HUB.unregister(client)
        logger.info("bridge: extension socket closed (%d attached)", HUB.client_count)

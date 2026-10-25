"""FastAPI dashboard — serves the HTML page, /api/status JSON, and the
WebSocket endpoint our browser extension dials into."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from .bridge import HUB, BridgeClient

if TYPE_CHECKING:
    from .dashboard import DashboardState

logger = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="GVCW Slot Monitor", docs_url=None, redoc_url=None)

_dashboard: DashboardState | None = None
_config: dict = {}


def bind(dashboard: "DashboardState", config: dict) -> None:
    global _dashboard, _config
    _dashboard = dashboard
    _config = config


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
    """Whether a browser extension is attached, for the dashboard/CLI."""
    return JSONResponse({"connected": HUB.connected, "clients": HUB.client_count})


@app.websocket("/bridge")
async def bridge_socket(websocket: WebSocket) -> None:
    """The browser extension connects here and answers CDP commands.

    Requests are correlated by id; the hub picks this client for the crawler's
    calls. Closing the socket (or the extension being disabled) fails any
    in-flight request rather than hanging a booking attempt.
    """
    await websocket.accept()
    client = HUB.register(
        BridgeClient(client_id=uuid.uuid4().hex, send_raw=websocket.send_text)
    )
    logger.info("bridge: extension socket opened (%d attached)", HUB.client_count)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                logger.debug("bridge: ignoring non-JSON frame")
                continue
            await client.handle_message(message)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("bridge: socket failed")
    finally:
        HUB.unregister(client)
        logger.info("bridge: extension socket closed (%d attached)", HUB.client_count)
"""FastAPI dashboard — serves the HTML page and /api/status JSON."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

if TYPE_CHECKING:
    from .dashboard import DashboardState

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
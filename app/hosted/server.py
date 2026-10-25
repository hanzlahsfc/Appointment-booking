"""Hosted client API + screen/input stream.

Serves the client dashboard (a static SPA destined for Vercel), the sessioned
JSON API, and one WebSocket per dashboard tab carrying screencast frames down
and validated input events up.

Every handler resolves the caller to exactly one provisioned client and only
ever touches that client's browser and records — ownership is structural, not
a parameter the request can influence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from ..config import Config
from ..store import Store
from . import auth
from .manager import BrowserHostManager, InputRejected

if TYPE_CHECKING:
    from .monitor import HostedMonitor
    from .store import HostedStore

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "client-dashboard"
MAX_WS_FRAME_BYTES = 16 * 1024
MAX_INFLIGHT_INPUTS = 8


class HostedRuntime:
    """Everything the hosted server needs, built once at serve time."""

    def __init__(self, cfg: Config, hstore: "HostedStore", store: Store,
                 manager: BrowserHostManager):
        self.cfg = cfg
        self.hstore = hstore
        self.store = store
        self.manager = manager
        self.monitors: dict[int, "HostedMonitor"] = {}
        self.login_limiter = auth.RateLimiter()

    def monitor_for(self, client_id: int) -> "HostedMonitor | None":
        return self.monitors.get(client_id)

    def feed_for(self, client_id: int):
        monitor = self.monitors.get(client_id)
        return monitor.feed if monitor else None


def create_app(runtime: HostedRuntime) -> FastAPI:
    app = FastAPI(title="Appointment Booker — Hosted", docs_url=None, redoc_url=None)
    app.state.runtime = runtime
    rt = runtime

    # Only needed once the dashboard lives on Vercel (a different origin).
    # Token auth means no cookies travel — allow_credentials stays off.
    if rt.cfg.hosted.dashboard_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(rt.cfg.hosted.dashboard_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    def _client_or_401(request: Request) -> dict | None:
        return auth.client_of(rt.hstore, auth.extract_token(request))

    def _public_client(client: dict) -> dict:
        return {
            "id": client["id"],
            "username": client["username"],
            "display_name": client["display_name"],
            "profile_name": client["profile_name"],
            "booking_url": client["booking_url"],
            "category": client["category"],
            "location": client["location"],
            "auto_book": client["auto_book"],
        }

    # ── auth ─────────────────────────────────────────────────────────────

    @app.post("/api/login")
    async def login(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        code = str(body.get("code", ""))
        key = request.client.host if request.client else "?"
        if not rt.login_limiter.allow(key):
            return JSONResponse({"error": "too many attempts — slow down"}, status_code=429)
        client = rt.hstore.authenticate_code(code)
        if client is None:
            rt.login_limiter.fail(key)
            return JSONResponse({"error": "invalid access code"}, status_code=401)
        rt.login_limiter.clear(key)
        token = rt.hstore.create_session(client["id"], rt.cfg.hosted.session_ttl_hours)
        response = JSONResponse({"token": token, "client": _public_client(client)})
        response.set_cookie(
            auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
            max_age=rt.cfg.hosted.session_ttl_hours * 3600,
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        token = auth.extract_token(request)
        if token:
            rt.hstore.revoke_session(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

    @app.get("/api/me")
    async def me(request: Request):
        client = _client_or_401(request)
        if client is None:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return JSONResponse({"client": _public_client(client)})

    # ── status + records ─────────────────────────────────────────────────

    @app.get("/api/status")
    async def status(request: Request):
        client = _client_or_401(request)
        if client is None:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        browser = rt.manager.browser_for(client["id"])
        feed = rt.feed_for(client["id"])
        return JSONResponse(
            {
                "client": _public_client(client),
                "browser": browser.status() if browser else None,
                "monitor": feed.snapshot() if feed else None,
            }
        )

    @app.get("/api/records")
    async def records(request: Request):
        client = _client_or_401(request)
        if client is None:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return JSONResponse(rt.hstore.records_for(client["profile_name"]))

    @app.post("/api/control")
    async def control(request: Request):
        client = _client_or_401(request)
        if client is None:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        browser = await rt.manager.ensure_browser(client)
        if body.get("action") == "release":
            browser.release_control()
        else:
            browser.take_control()
        return JSONResponse({"control": "manual" if browser.is_manual else "automation"})

    @app.post("/api/booking/complete")
    async def booking_complete(request: Request):
        """'I finished the form' — verify the confirmation marker is on the
        client's page, then record the held booking as booked.

        Never trusts the click: the row only flips to booked when the
        portal's own confirmation marker is visible.
        """
        client = _client_or_401(request)
        if client is None:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        monitor = rt.monitor_for(client["id"])
        if monitor is None:
            return JSONResponse({"error": "no monitor for this client"},
                                status_code=409)
        return JSONResponse(await monitor.check_completion_now())

    @app.post("/api/browser/home")
    async def browser_home(request: Request):
        """Send the client's browser back to their own portal booking page."""
        client = _client_or_401(request)
        if client is None:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        browser = await rt.manager.ensure_browser(client)
        page = await browser.ensure_page()
        try:
            await page.goto(client["booking_url"], wait_until="domcontentloaded",
                            timeout=20_000)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"ok": True, "url": page.url})

    # ── the stream ───────────────────────────────────────────────────────

    @app.websocket("/ws/screen")
    async def screen(websocket: WebSocket):
        client = auth.client_of(rt.hstore, auth.extract_ws_token(websocket))
        if client is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        browser = await rt.manager.ensure_browser(client)
        queue = browser.subscribe()
        feed = rt.feed_for(client["id"])

        async def _send(obj: dict) -> None:
            await websocket.send_text(json.dumps(obj))

        try:
            await _send(
                {
                    "type": "hello",
                    "client": _public_client(client),
                    "viewport": {
                        "w": rt.cfg.hosted.viewport_width,
                        "h": rt.cfg.hosted.viewport_height,
                    },
                    "browser": browser.status(),
                    "monitor": feed.snapshot() if feed else None,
                }
            )
            # The screencast only emits on repaint — seed this subscriber.
            await browser.capture_once()
        except Exception:
            browser.unsubscribe(queue)
            return

        budget = _InputBudget(rt.cfg.hosted.max_input_events_per_second)

        async def _writer() -> None:
            try:
                while True:
                    event = await queue.get()
                    await _send(event)
            except (WebSocketDisconnect, RuntimeError):
                return

        async def _reader() -> None:
            try:
                while True:
                    raw = await websocket.receive_text()
                    if len(raw) > MAX_WS_FRAME_BYTES:
                        await _send({"type": "error", "message": "frame too large"})
                        continue
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        await _send({"type": "error", "message": "invalid JSON"})
                        continue
                    if not isinstance(message, dict):
                        continue
                    await _handle_input(client, browser, message, _send, budget)
            except WebSocketDisconnect:
                return

        writer = asyncio.create_task(_writer())
        reader = asyncio.create_task(_reader())
        done, pending = await asyncio.wait(
            {writer, reader}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        browser.unsubscribe(queue)

    async def _handle_input(client, browser, message, send, budget) -> None:
        kind = message.get("type")
        try:
            if kind == "ping":
                await send({"type": "pong", "ts": time.time()})
            elif kind == "control":
                if message.get("action") == "release":
                    browser.release_control()
                elif message.get("action") == "take":
                    browser.take_control()
            elif kind == "navigate":
                if message.get("target") == "home":
                    page = await browser.ensure_page()
                    await page.goto(client["booking_url"],
                                    wait_until="domcontentloaded", timeout=20_000)
            elif kind in ("mouse", "wheel", "key", "text"):
                if not budget.spend():
                    return  # silently drop floods; the client is not waiting on them
                if kind == "mouse":
                    await browser.input_mouse(
                        str(message.get("event", "click")),
                        message.get("x"), message.get("y"),
                        str(message.get("button", "left")),
                        int(message.get("clicks", 1) or 1),
                    )
                elif kind == "wheel":
                    await browser.input_wheel(
                        message.get("x", 0), message.get("y", 0),
                        message.get("dx", 0), message.get("dy", 0),
                    )
                elif kind == "key":
                    await browser.input_key(
                        str(message.get("event", "press")), str(message.get("key", ""))
                    )
                else:
                    await browser.input_text(str(message.get("text", "")))
            else:
                await send({"type": "error", "message": f"unknown message {kind!r}"})
        except InputRejected as exc:
            await send({"type": "error", "message": str(exc)})
        except Exception as exc:
            logger.debug("input failed for client %s: %s", client["id"], exc)
            await send({"type": "error", "message": "input could not be applied"})

    # ── static dashboard ─────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"ok": True})

    @app.get("/")
    async def index():
        return FileResponse(DASHBOARD_DIR / "index.html")

    @app.get("/app.js")
    async def app_js():
        return FileResponse(DASHBOARD_DIR / "app.js")

    @app.get("/style.css")
    async def style_css():
        return FileResponse(DASHBOARD_DIR / "style.css")

    @app.get("/config.js")
    async def config_js():
        return FileResponse(DASHBOARD_DIR / "config.js")

    return app


class _InputBudget:
    """Per-connection token bucket so a hostile or buggy client cannot flood
    the browser with input events faster than a human could produce them."""

    def __init__(self, per_second: int):
        self.rate = max(5, per_second)
        self.allowance = float(self.rate)
        self.checked = time.monotonic()

    def spend(self) -> bool:
        now = time.monotonic()
        self.allowance = min(self.rate, self.allowance + (now - self.checked) * self.rate)
        self.checked = now
        if self.allowance < 1.0:
            return False
        self.allowance -= 1.0
        return True

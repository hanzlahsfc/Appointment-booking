"""Session-token authentication for the hosted API and stream.

Tokens come from HostedStore (random 256-bit, stored hashed). Every endpoint
resolves a token to exactly one client row; there is no "admin" token that
could cross clients. Both the Authorization header and the `ab_session` cookie
are accepted — the cookie is what the dashboard SPA uses, the header is for
scripts; the WebSocket accepts either because browsers cannot set headers on
the WS upgrade.
"""
from __future__ import annotations

import hmac
import logging
import time
from collections import defaultdict
from typing import Any

from fastapi import Request, WebSocket

from .store import HostedStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "ab_session"


class RateLimiter:
    """Tiny per-key failure throttle for the login endpoint."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        self.max_failures = max_failures
        self.window = window_seconds
        self._fails: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._fails[key] if now - t < self.window]
        self._fails[key] = recent
        return len(recent) < self.max_failures

    def fail(self, key: str) -> None:
        self._fails[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)


def extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


def extract_ws_token(websocket: WebSocket) -> str | None:
    """WS auth: cookie first (browser), then ?token= for non-browser clients."""
    token = websocket.cookies.get(SESSION_COOKIE)
    if token:
        return token
    provided = websocket.query_params.get("token")
    return provided if provided else None


def resolve_client(store: HostedStore, token: str | None) -> dict | None:
    if not token:
        return None
    return store.resolve_session(token)


def tokens_equal(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a, b)


def client_of(store: HostedStore, token: str | None) -> dict[str, Any] | None:
    """The one and only ownership check used by every hosted endpoint."""
    return resolve_client(store, token)

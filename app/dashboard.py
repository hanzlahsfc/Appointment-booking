"""In-process shared state that feeds the web dashboard."""
from __future__ import annotations

import threading
import time
from collections import deque


class DashboardState:
    """Thread-safe registry of live worker status, visible slots and the
    recent alert feed. Lives in the same process as the crawler workers; the
    FastAPI dashboard reads snapshots from it."""

    def __init__(self, max_alerts: int = 200):
        self._lock = threading.Lock()
        self._status: dict[str, dict] = {}
        self._slots: dict[str, list[dict]] = {}
        self._alerts: deque[dict] = deque(maxlen=max_alerts)
        self.started_at = time.time()

    # ---- writers (crawler workers) --------------------------------------

    def set_status(self, profile: str, **fields) -> None:
        with self._lock:
            self._status.setdefault(profile, {}).update(fields)
            self._status[profile]["ts"] = time.time()

    def set_slots(self, profile: str, slots: list[dict]) -> None:
        with self._lock:
            self._slots[profile] = sorted(
                slots, key=lambda s: (s.get("date", ""), s.get("time") or "")
            )

    def push_alert(self, slot: dict, alert_status: str) -> None:
        with self._lock:
            self._alerts.appendleft(
                {
                    "ts": time.time(),
                    "profile": slot.get("profile"),
                    "category": slot.get("category"),
                    "location": slot.get("location"),
                    "date": slot.get("date"),
                    "time": slot.get("time"),
                    "status": alert_status,
                }
            )

    # ---- readers (dashboard) --------------------------------------------

    def snapshot(self, app_cfg=None) -> dict:
        with self._lock:
            return {
                "started_at": self.started_at,
                "uptime": int(time.time() - self.started_at),
                "now": time.time(),
                "profiles": [
                    {"name": name, **self._status.get(name, {})}
                    for name in self._status
                ],
                "slots": {k: list(v) for k, v in self._slots.items()},
                "alerts": list(self._alerts),
                "config": app_cfg,
            }
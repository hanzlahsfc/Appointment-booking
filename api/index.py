"""Vercel serverless entrypoint for the client dashboard.

The crawler workers cannot run on Vercel (no persistent process, no
Chromium, no long-lived WebSocket), so this serves the same dashboard UI
and /api/status shape as app/web.py, seeded with the real profiles from
config.yaml. Slots and alerts stay empty until a real crawler backend
feeds the state — deploy the full monitor on a VPS (see deploy/).
"""
import json
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FALLBACK_PROFILES = [
    {"name": "Ahmed - Work Permit", "category": "national_d_employment", "location": "lahore"},
    {"name": "Ahmed - Premiere Launch", "category": "national_d_seasonal", "location": "islamabad"},
]
FALLBACK_POLL = 300
STARTED_AT = time.time()


def _load_profiles():
    try:
        import yaml

        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        poll = int(cfg.get("app", {}).get("poll_interval_seconds", FALLBACK_POLL))
        profiles = [
            {"name": p["name"], "category": p.get("category"), "location": p.get("location")}
            for p in cfg.get("profiles", [])
        ]
        return profiles or FALLBACK_PROFILES, poll
    except Exception:
        return FALLBACK_PROFILES, FALLBACK_POLL


def _snapshot():
    profiles, poll = _load_profiles()
    now = time.time()
    return {
        "started_at": STARTED_AT,
        "uptime": int(now - STARTED_AT),
        "now": now,
        "profiles": [
            {
                "name": p["name"],
                "category": p["category"],
                "location": p["location"],
                "new_count": 0,
                "slots_count": 0,
                "last_poll": None,
                "duration": None,
                "error": None,
                "ts": now,
                "booking": {},
            }
            for p in profiles
        ],
        "slots": {},
        "alerts": [],
        "bookings": [],
        "config": {"poll": poll, "demo": False, "dry_run": False},
    }


def _index_html():
    try:
        return (ROOT / "app" / "static" / "index.html").read_text()
    except Exception:
        return "<h1>GVCW Slot Monitor</h1><p>Dashboard assets unavailable.</p>"


class handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._send(200, json.dumps(_snapshot()), "application/json")
        elif path == "/api/bridge":
            self._send(200, json.dumps({"connected": False, "clients": 0}), "application/json")
        elif path in ("/", "/index.html"):
            self._send(200, _index_html(), "text/html; charset=utf-8")
        else:
            self._send(404, json.dumps({"detail": "Not Found"}), "application/json")

    def log_message(self, *args):
        pass

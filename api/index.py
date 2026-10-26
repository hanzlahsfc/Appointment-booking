"""Vercel serverless entrypoint — serves the real FastAPI dashboard app.

The crawler workers themselves cannot run on Vercel (no persistent process,
no Chromium, no long-lived WebSocket), so this exports the same app from
app/web.py bound to a fresh DashboardState seeded with the configured
profiles. /api/status returns real state — zero slots until a crawler
backend feeds it. For the full monitor, deploy on a VPS (see deploy/).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.dashboard import DashboardState
from app.web import app, bind

_cfg = load_config()
_dashboard = DashboardState()
for _p in _cfg.profiles:
    _dashboard.set_status(
        _p.name,
        category=_p.category,
        location=_p.location,
        new_count=0,
        slots_count=0,
        last_poll=None,
        duration=None,
        error=None,
    )
bind(
    _dashboard,
    {"poll": _cfg.app.poll_interval_seconds, "demo": False, "dry_run": _cfg.app.dry_run},
)

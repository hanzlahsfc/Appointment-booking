"""Timezone helpers, poll jitter, and name slugging."""
from __future__ import annotations

import random
from datetime import date, datetime
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")


def slugify(name: str) -> str:
    """Filesystem-safe slug used for credential and state files.
    'Ahmed - Work Permit' -> 'Ahmed_-_Work_Permit'"""
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


def now_pkt(tz: str = "Asia/Karachi") -> datetime:
    return datetime.now(ZoneInfo(tz))


def today_pkt(tz: str = "Asia/Karachi") -> date:
    return now_pkt(tz).date()


def jittered_delay(base_seconds: int, jitter_seconds: int) -> float:
    low = max(0, base_seconds - jitter_seconds)
    high = base_seconds + jitter_seconds
    return random.uniform(low, high)


def iso_pkt(dt: datetime) -> str:
    return dt.astimezone(PKT).isoformat(timespec="seconds")
"""Bounded availability monitoring through the operator's bridged tab.

Runs the driver's search-driven check on a bounded set of dates, a bounded
number of cycles, at a real interval — through the signed-in Chrome session
(the only path this egress allows). OTP and final submission are never in
scope here; this loop detects and reports only.

Bounds: --cycles (default 6), --scan-days (default 5, max 14),
--interval (default 90s, min 30). Any Bridge* stop (logout, challenge,
tab loss, off-origin nav) halts the loop immediately with the reason.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from .cdp_bridge import BridgeClient
from .gvcw import GvcwSiteDriver

logger = logging.getLogger(__name__)


async def run_monitor(
    tab_id: int,
    allowed_origin: str,
    *,
    category: str = "26",
    dates: list[str] | None = None,
    scan_days: int = 5,
    interval: int = 90,
    cycles: int = 6,
    applicant: dict | None = None,
    bridge_base: str = "http://localhost:1232",
    evidence_dir: Path | None = None,
    report=None,
) -> dict:
    """Bounded monitor. `report` is an optional callable(text) for progress."""
    say = report or logger.info
    client = BridgeClient(bridge_base)
    page = await client.page(tab_id, [allowed_origin], allow_writes=True)
    driver = GvcwSiteDriver(page, {"bookingfor": "0", "method": "1"})

    if not dates:
        days = max(1, min(scan_days, 14))
        today = date.today()
        dates = [(today + timedelta(days=i)).strftime("%d/%m/%Y")
                 for i in range(days)]
    interval = max(30, interval)
    cycles = max(1, min(cycles, 50))

    stats = {"cycles": 0, "searches": 0, "dates": dates, "slots": {},
             "errors": [], "bookable_total": 0}

    for cycle in range(1, cycles + 1):
        stats["cycles"] = cycle
        say(f"-- cycle {cycle}/{cycles} --")
        for dmy in dates:
            t0 = time.perf_counter()
            try:
                if applicant:
                    await driver.fill_applicant(applicant)
                await driver.apply_filters(None, category)
                await driver._search(dmy)
                grid = await driver._read_grid(dmy)
                dur = time.perf_counter() - t0
                stats["searches"] += 1
                stats["slots"][dmy] = grid
                stats["bookable_total"] += len(grid)
                say(f"  {dmy}: {len(grid)} bookable slot(s) [{dur:.1f}s]")
            except Exception as exc:
                stats["errors"].append(
                    {"cycle": cycle, "date": dmy,
                     "error": f"{type(exc).__name__}: {exc}"})
                say(f"  {dmy}: ERROR {type(exc).__name__}: {exc}")
                # Bridge* stops are fatal — the loop halts, never retries
                # past a structural failure (logout/challenge/tab loss).
                if type(exc).__name__.startswith("Bridge"):
                    raise
        if cycle < cycles:
            say(f"  next cycle in {interval}s")
            await asyncio.sleep(interval)

    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        out = evidence_dir / f"bridge-monitor-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.write_text(json.dumps(stats, indent=2))
        stats["evidence"] = str(out)
    return stats

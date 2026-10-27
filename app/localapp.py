"""Local single-user app: queue-driven monitoring through the bridged tab.

One process, one operator, one Chrome tab. The monitor walks the applicant
queue in order: for the current applicant it fills the form, searches the
date range, and on a bookable slot selects it and hands off — the operator
does OTP + captcha + the explicit Confirm-and-book. A verified
/appointments/result/{ref} marks the applicant booked and advances;
any ambiguous outcome parks the queue for review and never retries.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from .cdp_bridge import BridgeClient, BridgeError
from .gvcw import GvcwSiteDriver
from .queue import ApplicantQueue
from .site import SiteNotConfigured, SlotNotFound

logger = logging.getLogger(__name__)


def _dmy(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _daterange(date_from: str | None, date_to: str | None,
               default_days: int = 14) -> list[str]:
    def parse(s):
        from datetime import datetime
        try:
            return datetime.strptime(s, "%d/%m/%Y").date()
        except Exception:
            return None
    start = parse(date_from) or date.today()
    end = parse(date_to) or (start + timedelta(days=default_days))
    return [_dmy(start + timedelta(days=i))
            for i in range(max(0, (end - start).days))]


class LocalMonitor:
    def __init__(self, queue: ApplicantQueue, page_factory,
                 allowed_origin: str, *, interval: int = 60,
                 category: str = "26"):
        self.queue = queue
        self.page_factory = page_factory   # async callable -> gated page
        self.allowed_origin = allowed_origin
        self.interval = max(30, interval)
        self.category = category
        self.tab_id: int | None = None      # set at pair time
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.paused = False
        self.feed: dict = {"status": "idle", "events": [],
                           "stats": {"cycles": 0, "searches": 0,
                                     "latency_ms": None, "last_check": None}}

    def _say(self, status: str, **extra):
        self.feed["status"] = status
        self.feed.update(extra)
        ev = {"ts": time.time(), "status": status}
        ev.update({k: v for k, v in extra.items() if k != "state"})
        self.feed["events"].insert(0, ev)
        del self.feed["events"][100:]

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def pair(self, tab_id: int):
        self.tab_id = tab_id

    async def _run(self):
        while not self._stop.is_set():
            applicant = self.queue.current()
            if applicant is None:
                self._say("queue_finished", state={"done": True})
                return
            if self.paused:
                self._say("paused")
                await self._sleep(self.interval)
                continue
            if self.tab_id is None:
                self._say("awaiting_pair")
                await self._sleep(5)
                continue
            if applicant["status"] == "queued":
                self.queue.set_status(applicant["id"], "monitoring")
            if applicant["status"] in (
                    "selected_awaiting_human", "outcome_unknown"):
                self._say(applicant["status"], applicant_id=applicant["id"],
                          note="held — finish by hand or resolve")
                await self._sleep(self.interval)
                continue
            try:
                await self._cycle(applicant)
            except asyncio.CancelledError:
                raise
            except BridgeError as exc:
                self._say("bridge_error", error=str(exc))
                await self._sleep(self.interval)
            except Exception as exc:
                logger.exception("monitor cycle failed")
                self._say("error", error=str(exc))
                await self._sleep(self.interval)

    async def _sleep(self, s):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    async def _cycle(self, applicant: dict) -> None:
        page = await self.page_factory()
        driver = GvcwSiteDriver(page, {"bookingfor": "0", "method": "1"})

        category = applicant.get("category") or self.category
        dates = _daterange(applicant.get("date_from"),
                           applicant.get("date_to"))
        self._say("monitoring", applicant_id=applicant["id"], dates=dates)

        found = None
        for dmy in dates:
            t0 = time.perf_counter()
            if applicant.get("data"):
                try:
                    await driver.fill_applicant(applicant["data"])
                except Exception:
                    logger.warning("prefill failed", exc_info=True)
            await driver.apply_filters(None, category)
            try:
                await driver._search(dmy)
            except SiteNotConfigured as exc:
                self._say("search_error", date=dmy, error=str(exc))
                continue
            grid = await driver._read_grid(dmy)
            self.feed["stats"]["searches"] += 1
            self.feed["stats"]["latency_ms"] = round(
                (time.perf_counter() - t0) * 1000)
            self.feed["stats"]["last_check"] = time.time()
            if grid:
                found = {"date": dmy, "time": grid[0].split()[-1]}
                break
        if not found:
            self._say("monitoring", applicant_id=applicant["id"],
                      state={"bookable": 0, "dates": dates})
            await self._sleep(self.interval)
            return

        # ── a bookable slot: select it, prefill, alarm, hand off ────────
        slot = {"date": found["date"], "time": found["time"],
                "category": category}
        try:
            ok = await driver.locate({"date": found["date"],
                                      "time": found["time"]})
            if not ok:
                raise SlotNotFound("slot vanished before selection")
            await driver.reserve({"date": found["date"],
                                  "time": found["time"]})
        except (SlotNotFound, Exception) as exc:
            self._say("select_error", error=str(exc), slot=slot)
            return
        if applicant.get("data"):
            try:
                await driver.fill_applicant(applicant["data"])
            except Exception:
                logger.warning("prefill failed", exc_info=True)
        self.queue.set_status(applicant["id"], "selected_awaiting_human",
                              note=f"slot {found['date']} {found['time']}")
        self._say("selected_awaiting_human", applicant_id=applicant["id"],
                  slot=slot)

    # ── operator actions (dashboard) ─────────────────────────────────────

    async def request_otp(self) -> dict:
        driver = await self._driver()
        if driver is None or not isinstance(driver, GvcwSiteDriver):
            return {"ok": False, "error": "no bridged page"}
        err = await driver.request_otp()
        if err:
            return {"ok": False, "error": err}
        self._say("otp_sent")
        return {"ok": True}

    async def submit_otp(self, code: str) -> dict:
        driver = await self._driver()
        if driver is None:
            return {"ok": False, "error": "no bridged page"}
        try:
            await driver.enter_otp(code.strip())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    async def confirm_book(self) -> dict:
        applicant = self.queue.current()
        if not applicant or applicant["status"] != "selected_awaiting_human":
            return {"ok": False, "error": "no held booking to confirm"}
        self.queue.set_status(applicant["id"], "submitting")
        driver = await self._driver()
        if driver is None:
            return {"ok": False, "error": "no bridged page"}
        try:
            await driver.submit()
            outcome = await driver.submit_outcome()
        except Exception as exc:
            outcome = {"error": str(exc)}
        if outcome.get("ok"):
            ref = outcome["ok"]
            self.queue.set_status(applicant["id"], "booked",
                                  result_ref=ref)
            self._say("booked", applicant_id=applicant["id"], ref=ref)
            return {"ok": True, "confirmation_ref": ref}
        err = outcome.get("error") or "no confirmation — verify the page"
        self.queue.set_status(applicant["id"], "outcome_unknown",
                              note=err)
        self._say("outcome_unknown", applicant_id=applicant["id"], error=err)
        return {"ok": False, "error": err}

    async def _driver(self):
        if self.tab_id is None:
            return None
        page = await self.page_factory()
        return GvcwSiteDriver(page, {"bookingfor": "0", "method": "1"})

    def snapshot(self) -> dict:
        return {
            "status": self.feed.get("status"),
            "feed": self.feed,
            "queue": [q | {"data": {k: ("***" if k != "display" else v)
                                    for k, v in q["data"].items()}}
                      for q in self.queue.list()],
            "current": self.queue.current(),
            "tab_id": self.tab_id,
            "paused": self.paused,
        }

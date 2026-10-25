"""Per-client monitoring worker for the hosted build.

Mirrors CrawlerWorker's poll — navigate, verify session, scrape, diff, maybe
book — but drives the client's own hosted browser and follows the hosted rules:

  * while the client holds manual control, the worker never touches the page;
  * a bounced-to-login page is reported as `awaiting_login` instead of an
    error — in the hosted model the CLIENT logs in by hand through remote
    control, there are no stored portal credentials;
  * an unresolved or in-flight booking_log row parks polling exactly like the
    crawler (a slot may still be held), and that block survives restarts
    because it lives in SQLite;
  * assist-mode hand-off reaches the client as a stream event + dashboard
    status, not just the host's local alarm.

Booking guard rails all stay in BookingAgent/booking_log — this worker only
decides when it is safe to navigate.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..booking import BookingAgent, BookingStatus
from ..config import Config
from ..crawler import CrawlerWorker
from ..diff import select_bookable, sort_by_date
from ..site import BookingSelectors, PlaywrightSiteDriver, SiteNotConfigured
from ..store import Store
from ..util import iso_pkt, now_pkt
from .manager import BrowserHostManager

logger = logging.getLogger(__name__)

BLOCKED_RECHECK_SECONDS = 20


class ClientFeed:
    """The dashboard surface BookingAgent/CrawlerWorker push into, scoped to
    one client: every event also fans out to the client's live stream.

    Implements the same method names as DashboardState (set_status, set_slots,
    push_alert, set_booking, push_booking) so existing components work
    unchanged — but a ClientFeed can only ever carry its own client's data.
    """

    def __init__(self, browser_owner, profile_name: str):
        self._browser = browser_owner  # ClientBrowser — publishes to subscribers
        self.profile_name = profile_name
        self.state: dict = {
            "status": "starting",
            "message": "",
            "last_poll": None,
            "slots": [],
            "new_count": 0,
        }
        self.booking: dict = {}
        self.alerts: list[dict] = []
        self.bookings: list[dict] = []

    # DashboardState-compatible writers ───────────────────────────────────

    def set_status(self, profile: str, **fields) -> None:
        self.state.update(fields)
        self.state["ts"] = time.time()
        self._emit({"type": "monitor", "state": self._public_state()})

    def set_slots(self, profile: str, slots: list[dict]) -> None:
        self.state["slots"] = sorted(
            slots, key=lambda s: (s.get("date", ""), s.get("time") or "")
        )
        self._emit({"type": "monitor", "state": self._public_state()})

    def push_alert(self, slot: dict, alert_status: str) -> None:
        alert = {
            "ts": time.time(),
            "profile": slot.get("profile"),
            "category": slot.get("category"),
            "location": slot.get("location"),
            "date": slot.get("date"),
            "time": slot.get("time"),
            "status": alert_status,
        }
        self.alerts.insert(0, alert)
        del self.alerts[100:]
        self._emit({"type": "alert", "alert": alert})

    def set_booking(self, profile: str, **fields) -> None:
        self.booking.update(fields)
        self.booking["ts"] = time.time()
        self._emit({"type": "booking", "booking": dict(self.booking)})

    def push_booking(self, result: dict) -> None:
        entry = {
            "ts": time.time(),
            "profile": result.get("profile"),
            "status": result.get("status"),
            "stage": result.get("stage"),
            "date": (result.get("slot") or {}).get("date"),
            "time": (result.get("slot") or {}).get("time"),
            "confirmation_ref": result.get("confirmation_ref"),
            "error": result.get("error"),
        }
        self.bookings.insert(0, entry)
        del self.bookings[100:]
        self._emit({"type": "booking", "booking": entry})

    def notify_booking(self, result: dict) -> None:
        """Notifier-shaped sink: a booking result IS the client's alert."""
        self.push_booking(result)

    # ── helpers ──────────────────────────────────────────────────────────

    def _public_state(self) -> dict:
        return {
            "status": self.state.get("status"),
            "message": self.state.get("message", ""),
            "last_poll": self.state.get("last_poll"),
            "slots": self.state.get("slots", []),
            "new_count": self.state.get("new_count", 0),
        }

    def _emit(self, event: dict) -> None:
        self._browser.publish(event)

    def snapshot(self) -> dict:
        return {
            "monitor": self._public_state(),
            "booking": dict(self.booking),
            "alerts": list(self.alerts[:50]),
            "bookings": list(self.bookings[:50]),
        }


class HostedMonitor:
    """Poll loop bound to one client and their hosted browser."""

    def __init__(
        self,
        cfg: Config,
        client: dict,
        store: Store,
        manager: BrowserHostManager,
    ):
        self.cfg = cfg
        self.client = client
        self.profile_name = client["profile_name"]
        self.store = store
        self.manager = manager
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.feed: ClientFeed | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"hosted:{self.client['username']}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ── loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        browser = await self.manager.ensure_browser(self.client)
        self.feed = ClientFeed(browser, self.profile_name)
        selectors = BookingSelectors(
            **{
                k: v
                for k, v in (self.client.get("selectors") or {}).items()
                if k in BookingSelectors.__dataclass_fields__
            }
        )
        if isinstance(self.client.get("selectors", {}).get("token_inputs"), list):
            selectors.token_inputs = tuple(self.client["selectors"]["token_inputs"])

        interval = max(5, self.cfg.hosted.poll_interval_seconds)
        while not self._stop.is_set():
            try:
                await self._poll_once(browser, selectors)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("hosted poll failed for %s", self.profile_name)
                self.feed.set_status(
                    self.profile_name, status="error",
                    message=f"{type(exc).__name__}: {exc}", last_poll=time.time(),
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self, browser, selectors: BookingSelectors) -> None:
        profile = self.profile_name

        # 1. The client is driving — hands off entirely.
        if browser.is_manual:
            self.feed.set_status(profile, status="manual_control",
                                 message="client is in control; automation paused")
            return

        # 2. A slot may still be held — never navigate away from it. The check
        #    is the same DB-backed one the crawler uses, so it persists restarts.
        reason = self._block_reason()
        if reason:
            self.feed.set_status(profile, status="holding", message=reason)
            return

        page = await browser.ensure_page()
        driver = PlaywrightSiteDriver(page, selectors)

        t0 = time.perf_counter()
        await driver.open_booking_page(self.client["booking_url"])
        await driver.wait_for_waf()

        if await driver.is_login_page():
            # No stored credentials exist for hosted clients — the human on the
            # other end of the screencast does the portal sign-in.
            self.feed.set_status(
                profile, status="awaiting_login",
                message="portal sign-in required — take control and log in",
                last_poll=time.time(),
            )
            return

        if not selectors.configured or not selectors.slot_row:
            self.feed.set_status(
                profile, status="error",
                message="monitoring selectors are not configured for this client",
                last_poll=time.time(),
            )
            return

        await driver.apply_filters(self.client["location"], self.client["category"])
        rows = await driver.list_slots()
        slots = self._normalize(rows)
        dur = time.perf_counter() - t0

        new_slots = self._process(slots)
        self.feed.set_status(profile, status="ok", message="", last_poll=time.time())
        self.feed.set_slots(profile, slots)
        logger.info(
            "HOSTED OK | client=%s | slots=%d | new=%d | dur=%.1fs",
            self.client["username"], len(slots), len(new_slots), dur,
        )

        if new_slots and not browser.is_manual:
            await self._maybe_book(browser, driver, new_slots)

    # ── slot processing + booking ────────────────────────────────────────

    def _normalize(self, rows: list[str]) -> list[dict]:
        """Same label formats as the crawler (YYYY-MM-DD, DD.MM.YYYY, ...)."""
        out = []
        for raw in rows:
            parsed = CrawlerWorker._parse_slot_label(raw)
            if parsed is None:
                continue
            date_str, time_str = parsed
            out.append(
                {
                    "profile": self.profile_name,
                    "location": self.client["location"],
                    "category": self.client["category"],
                    "date": date_str,
                    "time": time_str,
                    "raw": raw,
                }
            )
        return out

    def _process(self, slots: list[dict]) -> list[dict]:
        now = iso_pkt(now_pkt())
        new_slots = self.store.upsert_slots(slots, seen_at=now)
        scope = {(s["date"], s["time"] or "") for s in slots}
        self.store.deactivate_missing(
            self.profile_name, self.client["location"], self.client["category"],
            scope, seen_at=now,
        )
        new_slots = sort_by_date(new_slots)
        for slot in new_slots:
            slot["email"] = ""
            slot["booking_url"] = self.client["booking_url"]
            # The dashboard event is the alert; recording it in alert_log keeps
            # the books consistent and marks the slot so it never re-alerts.
            self.store.mark_alert_attempted([slot])
            self.store.mark_alerted([slot])
            self.store.log_alert(
                slot, sent_at=now,
                subject=f"NEW SLOT: {slot['category']} @ {slot['location']} — {slot['date']}",
                status="dashboard",
            )
            if self.feed:
                self.feed.push_alert(slot, "dashboard")
        return new_slots

    async def _maybe_book(self, browser, driver, new_slots: list[dict]) -> None:
        if not self.cfg.booking_enabled_for(self.profile_name):
            return
        slot = select_bookable(new_slots, {})
        if slot is None:
            return
        # Re-check immediately before acting: a client who grabbed control
        # during this poll must not find the agent clicking under them.
        if browser.is_manual:
            return
        agent = BookingAgent(
            self.cfg,
            self._profile_dict(),
            self.store,
            dashboard=self.feed,
            notifier=self.feed,
        )
        result = await agent.book(slot, driver)
        if result.status == BookingStatus.NEEDS_HUMAN.value:
            # The remote hand-off: the client sees this on their stream and in
            # the dashboard, then takes control and finishes the form. Polling
            # stays parked via the unresolved booking row until resolved.
            self.feed.set_status(
                self.profile_name, status="needs_human",
                message="a slot was claimed — take control and finish the form",
            )

    def _profile_dict(self) -> dict:
        return {
            "name": self.profile_name,
            "email": "",
            "category": self.client["category"],
            "location": self.client["location"],
            "booking_url": self.client["booking_url"],
            "auto_book": self.client.get("auto_book", False),
            "prefer": {},
        }

    def _block_reason(self) -> str | None:
        open_row = self.store.open_booking(self.profile_name)
        if open_row:
            return (
                f"booking attempt #{open_row['id']} never finished — a slot may "
                "still be held; resolve it before polling resumes"
            )
        unresolved = self.store.unresolved_booking(self.profile_name)
        if unresolved:
            return (
                f"booking attempt #{unresolved['id']} ended {unresolved['status']} "
                "— a slot may still be held; resolve it before polling resumes"
            )
        return None

"""CrawlerWorker: per-profile fetch + normalize + diff loop.

Supports a --demo mode that fabricates realistic slots so the web
dashboard can be screenshotted without real credentials.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext

from .alarm import Alarm, AlarmConfig
from .booking import BookingAgent, BookingStatus
from .browser import (
    BrowserSession,
    IncapsulaHandler,
    create_browser,
    launch_persistent,
    new_context,
)
from .config import Config
from .diff import select_bookable, sort_by_date
from .notifier import Notifier
from .site import PlaywrightSiteDriver, load_selectors
from .session import (
    check_session_still_valid,
    ensure_logged_in,
    has_storage_state,
    storage_state_path,
)
from .store import Store
from .util import iso_pkt, jittered_delay, now_pkt, PKT, slugify

if TYPE_CHECKING:
    from .dashboard import DashboardState

logger = logging.getLogger(__name__)

SLOT_SELECTOR_HINT = None  # filled by recon (§11)

# ── demo slot generation ──────────────────────────────────────────────────
_DEMO_TIMES = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "14:00", "14:30"]

# Work permit / National D (Employment & Rest) and premiere-launch releases
# typically open Thu–Fri; keep demo slots inside that window.
_DEMO_DAYS_AHEAD = range(15, 25)


def _demo_seed(profile: dict) -> list[dict]:
    """Create an initial realistic set of slots for demo mode.

    Only work-permit / premiere-launch (National D) slots are generated —
    no Schengen Type C.
    """
    today = date.today()
    num = random.randint(3, 6)
    days = sorted(random.sample(list(_DEMO_DAYS_AHEAD), num))
    return [
        {
            "profile": profile["name"],
            "location": profile["location"],
            "category": profile["category"],
            "date": (today + timedelta(days=d)).isoformat(),
            "time": random.choice(_DEMO_TIMES),
            "raw": f"{(today+timedelta(days=d)).isoformat()} {random.choice(_DEMO_TIMES)}",
        }
        for d in days
    ]


class CrawlerWorker:
    def __init__(
        self,
        cfg: Config,
        profile: dict,
        store: Store,
        notifier: Notifier,
        dashboard: "DashboardState | None" = None,
        demo: bool = False,
    ):
        self.cfg = cfg
        self.profile = profile
        self.store = store
        self.notifier = notifier
        self.dashboard = dashboard
        self.demo = demo
        self._browser: Browser | None = None
        self._session: BrowserSession | None = None
        self._alarm = Alarm(AlarmConfig(seconds=cfg.booking.alarm_seconds))
        self._holding = False
        self._seen_keys: set[tuple] = set()
        # demo state
        self._demo_slots: list[dict] = []
        self._demo_polls = 0

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self.demo:
            return
        profile_dir = Path(self.cfg.booking.profile_dir)
        if profile_dir.exists() and any(profile_dir.iterdir()):
            # warm profile from the recon/login pass: no credentials needed
            self._session = await launch_persistent(
                profile_dir, headless=self.cfg.headless, proxy=self.cfg.proxy
            )
        else:
            logger.warning(
                "no warm Chrome profile at %s — falling back to storage_state login "
                "(reCAPTCHA may block this; run recon.py once to create the profile)",
                profile_dir,
            )
            self._browser = await create_browser(headless=self.cfg.headless)

    async def stop(self) -> None:
        if self._session:
            await self._session.aclose()
            self._session = None
        if self._browser:
            await self._browser.close()
            self._browser = None

    # ── main loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        if self.demo:
            await self._run_demo()
            return
        state_path = storage_state_path(self.cfg.state_dir, self.profile["name"])
        creds_file = self.cfg.creds_dir / f"{self._slug(self.profile['name'])}.json"
        if self._session is not None:
            context = self._session.context
        else:
            context = await self._make_context(state_path, creds_file)
        try:
            while True:
                delay = jittered_delay(
                    self.cfg.app.poll_interval_seconds,
                    self.cfg.app.jitter_seconds,
                )
                await asyncio.sleep(delay)
                await self._poll_once(context, state_path, creds_file)
        finally:
            await context.close()

    async def _run_demo(self) -> None:
        """Demo loop: fabricate slots so the dashboard is populated."""
        self._demo_slots = _demo_seed(self.profile)
        self._up_dash(new_count=len(self._demo_slots), slots=list(self._demo_slots))
        if self.dashboard:
            for slot in self._demo_slots:
                slot["email"] = self.profile.get("email")
                slot["booking_url"] = self.profile.get("booking_url")
                self.dashboard.push_alert(slot, "sent" if not self.notifier.dry_run else "dry-run")
        while True:
            await asyncio.sleep(jittered_delay(
                self.cfg.app.poll_interval_seconds,
                self.cfg.app.jitter_seconds,
            ))
            self._demo_polls += 1
            added = self._tick_demo_slots()
            self._up_dash(new_count=len(added), slots=list(self._demo_slots))
            if self.dashboard:
                for slot in added:
                    slot["email"] = self.profile.get("email")
                    slot["booking_url"] = self.profile.get("booking_url")
                    self.dashboard.push_alert(slot, "sent" if not self.notifier.dry_run else "dry-run")
            logger.info(
                "DEMO | profile=%s | slots=%d | poll=%d",
                self.profile["name"],
                len(self._demo_slots),
                self._demo_polls,
            )

    def _tick_demo_slots(self) -> list[dict]:
        """Simulate slot changes for demo mode — every 3 polls, tweak a slot.
        Returns the newly-added slots (to feed the alert queue)."""
        if self._demo_polls % 3 != 0:
            return []
        action = random.choice(["add", "remove", "shift"])
        if action == "add" and len(self._demo_slots) < 8:
            today = date.today()
            day = random.choice(list(_DEMO_DAYS_AHEAD))
            t = random.choice(_DEMO_TIMES)
            slot = {
                "profile": self.profile["name"],
                "location": self.profile["location"],
                "category": self.profile["category"],
                "date": (today + timedelta(days=day)).isoformat(),
                "time": t,
                "raw": f"{(today+timedelta(days=day)).isoformat()} {t}",
            }
            self._demo_slots.append(slot)
            return [slot]
        if action == "remove" and len(self._demo_slots) > 2:
            self._demo_slots.pop(random.randrange(len(self._demo_slots)))
        elif self._demo_slots:
            s = random.choice(self._demo_slots)
            s["time"] = random.choice(_DEMO_TIMES)
            s["raw"] = f"{s['date']} {s['time']}"
        return []

    def _up_dash(self, new_count: int, slots: list[dict]) -> None:
        if self.dashboard:
            self.dashboard.set_status(
                self.profile["name"],
                category=self.profile["category"],
                location=self.profile["location"],
                new_count=new_count,
                slots_count=len(slots),
                last_poll=time.time(),
                duration=None,
                error=None,
            )
            self.dashboard.set_slots(self.profile["name"], slots)

    def _up_dash_error(self, err: str) -> None:
        if self.dashboard:
            self.dashboard.set_status(
                self.profile["name"],
                category=self.profile["category"],
                location=self.profile["location"],
                error=err,
                last_poll=time.time(),
            )

    # ── helpers (real mode) ───────────────────────────────────────────────

    async def _make_context(self, state_path: Path, creds_file: Path) -> BrowserContext:
        context = await new_context(
            self._browser,
            storage_state=state_path,
            proxy=self.cfg.proxy,
        )
        if not has_storage_state(self.cfg.state_dir, self.profile["name"]):
            await ensure_logged_in(context, creds_file, self.profile["name"], state_path)
        return context

    async def _poll_once(self, context: BrowserContext, state_path: Path, creds_file: Path) -> None:
        t0 = time.perf_counter()
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            url = self.profile["booking_url"]
            await page.goto(url, wait_until="domcontentloaded")

            if not await check_session_still_valid(page):
                logger.warning("profile %s: session expired, re-logging in", self.profile["name"])
                await ensure_logged_in(context, creds_file, self.profile["name"], state_path)
                await page.goto(url, wait_until="domcontentloaded")

            slots = await self._fetch_slots(page)
            dur = time.perf_counter() - t0
            new_slots = self._process_slots(slots)
            new_count = len(new_slots)
            self._up_dash(new_count=new_count, slots=slots)
            logger.info(
                "OK | profile=%s | slots=%d | new=%d | dur=%.1fs",
                self.profile["name"],
                len(slots),
                new_count,
                dur,
            )
            if new_slots:
                await self._maybe_book(page, new_slots)
        except Exception as e:
            dur = time.perf_counter() - t0
            self._up_dash_error(str(e))
            logger.exception("poll failed for profile %s: %s", self.profile["name"], e)

    def _process_slots(self, slots: list[dict]) -> list[dict]:
        """Returns list of NEW (alertable) slots for this poll."""
        now = iso_pkt(now_pkt())
        if self.notifier.dry_run:
            cur_keys = {self._key(s) for s in slots}
            unseen = [s for s in slots if self._key(s) not in self._seen_keys]
            self._seen_keys |= cur_keys
            for slot in sort_by_date(unseen):
                slot["email"] = self.profile.get("email")
                slot["booking_url"] = self.profile.get("booking_url")
                if self.dashboard:
                    self.dashboard.push_alert(slot, "dry-run")
                logger.info("[dry-run] NEW slot: %s @ %s on %s", slot["category"], slot["location"], slot["date"])
            return unseen

        new_slots = self.store.upsert_slots(slots, seen_at=now)
        scope = {(s["date"], s["time"]) for s in slots}
        if scope:
            self.store.deactivate_missing(
                self.profile["name"],
                self.profile["location"],
                self.profile["category"],
                scope,
                seen_at=now,
            )
        new_slots = sort_by_date(new_slots)
        for slot in new_slots:
            slot["email"] = self.profile.get("email")
            slot["booking_url"] = self.profile.get("booking_url")
            asyncio.get_running_loop().create_task(self._dispatch_alert(slot))
        return new_slots

    @staticmethod
    def _key(slot: dict) -> tuple:
        return (slot["profile"], slot["location"], slot["category"], slot["date"], slot["time"])

    async def _dispatch_alert(self, slot: dict) -> None:
        """Send the alert and record it."""
        if self.notifier.dry_run:
            await self.notifier.send(slot, auto_book=self.cfg.booking.mode != "off")
            if self.dashboard:
                self.dashboard.push_alert(slot, "dry-run")
            return
        ok = await self.notifier.send(slot, auto_book=self.cfg.booking.mode != "off")
        sent_at = iso_pkt(now_pkt())
        subject = self.notifier.build_subject(slot)
        if ok:
            self.store.mark_alerted([slot])
        self.store.log_alert(slot, sent_at=sent_at, subject=subject, status="sent" if ok else "failed")
        if self.dashboard:
            self.dashboard.push_alert(slot, "sent" if ok else "failed")

    # ── auto-booking ──────────────────────────────────────────────────────

    async def _maybe_book(self, page, new_slots: list[dict]) -> None:
        """Claim the best new slot if this profile opted in.

        Runs inline (awaited) rather than as a background task: one Playwright
        page cannot be driven concurrently, and the next poll must not navigate
        away from a slot we are holding for a human.
        """
        if not self.cfg.booking_enabled_for(self.profile["name"]):
            return
        slot = select_bookable(new_slots, self.profile.get("prefer"))
        if slot is None:
            logger.info("booking on, but no slot matched this profile's preferences")
            return

        driver = PlaywrightSiteDriver(page, load_selectors(self.cfg.booking.selectors_file))
        agent = BookingAgent(
            self.cfg,
            self.profile,
            self.store,
            alarm=self._alarm,
            dashboard=self.dashboard,
            notifier=self.notifier,
        )
        result = await agent.book(slot, driver)
        if result.status == BookingStatus.NEEDS_HUMAN.value:
            await self._hold_for_human(result)
        elif result.status == BookingStatus.ABORTED_UNKNOWN_STATE.value:
            # never walk away from a possibly-held slot
            await self._hold_for_human(result)

    async def _hold_for_human(self, result) -> None:
        """Stop polling so the page is left exactly as the claim left it."""
        self._holding = True
        logger.warning(
            "HOLDING for human hand-off: %s %s | status=%s | the browser window is left "
            "open and polling is paused — finish or check the booking, then restart the app",
            result.slot.get("date"),
            result.slot.get("time") or "",
            result.status,
        )
        while self._holding:
            await asyncio.sleep(30)
            logger.warning(
                "still holding the claimed slot (%s) — polling stays paused until restart",
                result.slot.get("date"),
            )

    # ── slot extraction (recon-driven, §11) ──────────────────────────────

    async def _fetch_slots(self, page) -> list[dict]:
        inc = IncapsulaHandler()
        await inc.wait_for_clear(page)
        raw_slots = await self._scrape_dom(page)
        return self._normalize_raw_slots(raw_slots)

    async def _scrape_dom(self, page) -> list[dict]:
        harvested = []
        try:
            rows = page.locator(SLOT_SELECTOR_HINT or ".slot-row")
            count = await rows.count()
            for i in range(count):
                text = (await rows.nth(i).inner_text()).strip()
                if text:
                    harvested.append({"raw": text})
        except Exception:
            harvested = []
        return harvested

    def _normalize_raw_slots(self, raw: list[dict]) -> list[dict]:
        out = []
        for item in raw:
            parsed = self._parse_slot_label(item.get("raw", ""))
            if parsed is None:
                continue
            date_str, time_str = parsed
            out.append(
                {
                    "profile": self.profile["name"],
                    "location": self.profile["location"],
                    "category": self.profile["category"],
                    "date": date_str,
                    "time": time_str,
                    "raw": item.get("raw", ""),
                }
            )
        return out

    def _parse_slot_label(self, raw: str) -> tuple[str, str] | None:
        parts = raw.split()
        if not parts:
            return None
        date_str = parts[0]
        time_str = parts[1] if len(parts) > 1 and ":" in parts[1] else None
        if len(date_str) != 10:
            return None
        return date_str, time_str

    def _slug(self, name: str) -> str:
        return slugify(name)
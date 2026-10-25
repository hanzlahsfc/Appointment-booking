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

import re

from playwright.async_api import Browser, BrowserContext

from .alarm import Alarm, AlarmConfig
from .booking import BookingAgent, BookingStatus
from .bridge import (
    HUB,
    BridgeError,
    BridgeNotConnected,
    BridgeTimeout,
    TabInUse,
)
from .browser import (
    BrowserSession,
    create_browser,
    launch_persistent,
    new_context,
    WAFBlocked,
    WAFError,
)
from .config import Config
from .diff import select_bookable, sort_by_date
from .notifier import Notifier
from .site import (
    BridgeSiteDriver,
    PickerMismatch,
    PlaywrightSiteDriver,
    SiteNotConfigured,
    load_selectors,
)
from .session import (
    SessionExpired,
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

#: How often a blocked worker re-checks whether its held booking was resolved.
BLOCKED_RECHECK_SECONDS = 30

_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DMY_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")

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
        if self.demo or self.cfg.app.driver == "bridge":
            # bridge mode launches nothing — the operator's browser is the fleet
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
        if self.cfg.app.driver == "bridge":
            await self._run_loop(self._poll_once_bridge)
            return
        state_path = storage_state_path(self.cfg.state_dir, self.profile["name"])
        creds_file = self.cfg.creds_dir / f"{self._slug(self.profile['name'])}.json"
        if self._session is not None:
            context = self._session.context
        else:
            context = await self._make_context(state_path, creds_file)
        try:
            await self._run_loop(lambda: self._poll_once_playwright(context, state_path, creds_file))
        finally:
            await context.close()

    async def _run_loop(self, poll) -> None:
        """Poll immediately on startup, then every interval ± jitter.

        Before *any* navigation the worker checks for a booking that may still
        be holding a slot: an unresolved hand-off, an ambiguous attempt, or an
        in-flight attempt orphaned by a crash. While such a row exists, polling
        stays parked — navigating the tab would disturb the held page.
        """
        while True:
            reason = self._poll_block_reason()
            if reason:
                self._up_dash_error(reason)
                logger.warning("polling blocked for %s: %s", self.profile["name"], reason)
                await asyncio.sleep(BLOCKED_RECHECK_SECONDS)
                continue
            await poll()
            await asyncio.sleep(
                jittered_delay(
                    self.cfg.app.poll_interval_seconds,
                    self.cfg.app.jitter_seconds,
                )
            )

    def _poll_block_reason(self) -> str | None:
        """Why this worker must not navigate a tab right now, else None."""
        name = self.profile["name"]
        open_row = self.store.open_booking(name)
        if open_row:
            return (
                f"booking attempt #{open_row['id']} for {open_row['date']} "
                f"{open_row.get('time') or ''} never finished "
                f"(status={open_row['status']}) — a slot may still be held; "
                "resolve it with --resolve-booking before polling resumes"
            )
        unresolved = self.store.unresolved_booking(name)
        if unresolved:
            return (
                f"booking attempt #{unresolved['id']} for {unresolved['date']} "
                f"ended {unresolved['status']} — a slot may still be held; "
                "resolve it with --resolve-booking before polling resumes"
            )
        return None

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

    def _load_selectors(self):
        """Reloaded every poll: dropping in recon/booking-selectors.json after a
        recon pass must not require a restart."""
        return load_selectors(self.cfg.booking.selectors_file)

    async def _poll_once_bridge(self) -> None:
        """One poll through the operator's signed-in browser."""
        t0 = time.perf_counter()
        try:
            client = HUB.client()
            tab_id = self.profile.get("tab_id")
            if tab_id is None:
                tab_id = client.current_tab()
            driver = BridgeSiteDriver(client, self._load_selectors(), tab_id=tab_id)
            await self._poll_via_driver(driver)
        except Exception as e:
            self._up_dash_error(self._explain_error(e))
            logger.warning(
                "poll failed for profile %s: %s (%.1fs)",
                self.profile["name"], self._explain_error(e), time.perf_counter() - t0,
            )

    async def _poll_once_playwright(self, context: BrowserContext, state_path: Path, creds_file: Path) -> None:
        t0 = time.perf_counter()
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            url = self.profile["booking_url"]
            await page.goto(url, wait_until="domcontentloaded")

            if not await check_session_still_valid(page):
                logger.warning("profile %s: session expired, re-logging in", self.profile["name"])
                await ensure_logged_in(context, creds_file, self.profile["name"], state_path)
                await page.goto(url, wait_until="domcontentloaded")

            driver = PlaywrightSiteDriver(page, self._load_selectors())
            await self._poll_body(driver)
        except Exception as e:
            self._up_dash_error(self._explain_error(e))
            logger.exception("poll failed for profile %s: %s", self.profile["name"], e)

    @staticmethod
    def _explain_error(exc: Exception) -> str:
        """Operator-facing one-liner for every expected failure mode."""
        if isinstance(exc, BridgeNotConnected):
            return f"extension not ready: {exc}"
        if isinstance(exc, TabInUse):
            return str(exc)
        if isinstance(exc, BridgeTimeout):
            return f"extension did not answer in time: {exc}"
        if isinstance(exc, SessionExpired):
            return str(exc)
        if isinstance(exc, WAFBlocked):
            return f"portal is blocking this browser: {exc}"
        if isinstance(exc, WAFError):
            return f"portal challenge did not clear: {exc}"
        if isinstance(exc, SiteNotConfigured):
            return f"selectors not configured: {exc}"
        if isinstance(exc, PickerMismatch):
            return str(exc)
        if isinstance(exc, BridgeError):
            return f"extension error: {exc}"
        return f"{type(exc).__name__}: {exc}"

    async def _poll_via_driver(self, driver) -> None:
        """Poll using a bridge-backed driver. Claims the tab for the whole poll
        so a second worker cannot navigate it mid-operation — including while
        a booking is held for human hand-off."""
        HUB.claim_tab(driver.tab_id, self.profile["name"])
        try:
            await self._poll_body(driver)
        finally:
            HUB.release_tab(driver.tab_id, self.profile["name"])

    async def _poll_body(self, driver) -> None:
        """The shared poll: navigate → verify session → scrape → diff → act.

        Reaching _process_slots means the picker applied our filters, so its
        results (including 'no rows') are trustworthy — see apply_filters.
        """
        t0 = time.perf_counter()
        await driver.open_booking_page(self.profile["booking_url"])
        await driver.wait_for_waf()
        if await driver.is_login_page():
            raise SessionExpired(
                "the portal bounced to sign-in — log in again in the driven tab"
            )
        slots = await self._fetch_slots(driver)
        dur = time.perf_counter() - t0
        new_slots = self._process_slots(slots)
        self._up_dash(new_count=len(new_slots), slots=slots)
        logger.info(
            "OK | profile=%s | slots=%d | new=%d | dur=%.1fs",
            self.profile["name"], len(slots), len(new_slots), dur,
        )
        if new_slots:
            await self._maybe_book(driver, new_slots)

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
        # Reaching here means the picker's filters verified — an empty scrape
        # is a real "no slots", not a broken page, so deactivation is safe.
        scope = {(s["date"], s["time"] or "") for s in slots}
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
        self.store.mark_alert_attempted([slot])
        ok = await self.notifier.send(slot, auto_book=self.cfg.booking.mode != "off")
        sent_at = iso_pkt(now_pkt())
        subject = self.notifier.build_subject(slot)
        if ok:
            self.store.mark_alerted([slot])
        self.store.log_alert(slot, sent_at=sent_at, subject=subject, status="sent" if ok else "failed")
        if self.dashboard:
            self.dashboard.push_alert(slot, "sent" if ok else "failed")

    # ── auto-booking ──────────────────────────────────────────────────────

    async def _maybe_book(self, driver, new_slots: list[dict]) -> None:
        """Claim the best new slot if this profile opted in.

        Runs inline (awaited) rather than as a background task: one tab cannot
        be driven concurrently, and the next poll must not navigate away from a
        slot we are holding for a human.
        """
        if not self.cfg.booking_enabled_for(self.profile["name"]):
            return
        slot = select_bookable(new_slots, self.profile.get("prefer"))
        if slot is None:
            logger.info("booking on, but no slot matched this profile's preferences")
            return

        agent = BookingAgent(
            self.cfg,
            self.profile,
            self.store,
            alarm=self._alarm,
            dashboard=self.dashboard,
            notifier=self.notifier,
        )
        result = await agent.book(slot, driver)
        # Never let the next poll navigate away from a slot that may be held.
        # `claimed` covers needs_human and aborted_unknown_state; a booked slot
        # needs no further polling either, but it must not sit in the hold loop.
        if result.claimed and result.status != BookingStatus.BOOKED.value:
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

    async def _fetch_slots(self, driver) -> list[dict]:
        """Verified scrape: filters applied or we do not trust the rows."""
        selectors = driver.selectors
        if selectors is None or not selectors.configured or not selectors.slot_row:
            raise SiteNotConfigured(
                "monitoring selectors are not configured — run recon.py and "
                "fill recon/booking-selectors.json"
            )
        await driver.apply_filters(self.profile["location"], self.profile["category"])
        rows = await driver.list_slots()
        return self._normalize_raw_slots(rows)

    def _normalize_raw_slots(self, rows: list[str]) -> list[dict]:
        out = []
        for raw in rows:
            parsed = self._parse_slot_label(raw)
            if parsed is None:
                logger.debug("unparsed slot row, skipped: %r", raw[:80])
                continue
            date_str, time_str = parsed
            out.append(
                {
                    "profile": self.profile["name"],
                    "location": self.profile["location"],
                    "category": self.profile["category"],
                    "date": date_str,
                    "time": time_str,
                    "raw": raw,
                }
            )
        return out

    @staticmethod
    def _parse_slot_label(raw: str) -> tuple[str, str] | None:
        """Extract (YYYY-MM-DD, HH:MM|None) from a slot row's text.

        Accepts ISO dates anywhere in the label plus DD.MM.YYYY / DD/MM/YYYY;
        sanity-checks month/day so a stray number cannot masquerade as a date.
        """
        m = _ISO_DATE.search(raw)
        if m:
            date_str = m.group(1)
        else:
            m = _DMY_DATE.search(raw)
            if not m:
                return None
            day, month, year = (int(g) for g in m.groups())
            year += 2000 if year < 100 else 0
            if not (1 <= month <= 12 and 1 <= day <= 31):
                return None
            date_str = f"{year:04d}-{month:02d}-{day:02d}"
        t = _TIME.search(raw)
        time_str = f"{int(t.group(1)):02d}:{t.group(2)}" if t else None
        return date_str, time_str

    def _slug(self, name: str) -> str:
        return slugify(name)
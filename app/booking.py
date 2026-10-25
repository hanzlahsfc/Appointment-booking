"""Booking agent: detect → claim → hand off (assist) or submit (auto).

Design rules that matter more than the code:

  * Every attempt is claimed in booking_log first, so a slot can never be
    claimed twice even if two polls or two workers race.
  * In assist mode the agent clicks reserve and STOPS. The browser window is left
    exactly as it is so a human can finish the form — the alarm exists to make
    sure they notice.
  * A failure after the reserve click is reported as `aborted_unknown_state`,
    never retried blindly: the slot may already be held.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from .alarm import Alarm, AlarmConfig
from .config import Config
from .site import SiteDriver, SiteNotConfigured, SlotNotFound, load_selectors
from .store import Store
from .util import iso_pkt, now_pkt, slugify

logger = logging.getLogger(__name__)


class BookingStatus(str, Enum):
    DISABLED = "disabled"
    DRY_RUN = "dry_run"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    RATE_LIMITED = "rate_limited"
    SLOT_GONE = "slot_gone"
    NEEDS_HUMAN = "needs_human"
    BOOKED = "booked"
    FAILED = "failed"
    ABORTED_UNKNOWN_STATE = "aborted_unknown_state"


@dataclass
class BookingResult:
    status: str
    profile: str
    slot: dict
    stage: str
    mode: str
    confirmation_ref: str | None = None
    evidence_path: str | None = None
    error: str | None = None
    booking_id: int | None = None
    started_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["slot"] = dict(self.slot)
        return data

    @property
    def claimed(self) -> bool:
        """True when the slot may now be held (reserve was clicked)."""
        return self.status in (
            BookingStatus.NEEDS_HUMAN.value,
            BookingStatus.BOOKED.value,
            BookingStatus.ABORTED_UNKNOWN_STATE.value,
        )


class BookingAgent:
    def __init__(
        self,
        cfg: Config,
        profile: dict,
        store: Store,
        alarm: Alarm | None = None,
        dashboard: Any | None = None,
        notifier: Any | None = None,
    ):
        self.cfg = cfg
        self.profile = profile
        self.store = store
        self.dashboard = dashboard
        self.notifier = notifier
        self.alarm = alarm or Alarm(AlarmConfig(seconds=cfg.booking.alarm_seconds))

    # ── public ───────────────────────────────────────────────────────────

    async def book(self, slot: dict, driver: SiteDriver) -> BookingResult:
        started = iso_pkt(now_pkt())
        mode = self.cfg.booking.mode

        guard = self._guard(slot, started, mode)
        if guard is not None:
            return guard

        if mode == "auto":
            # Everything unattended booking needs is checked before anything is
            # clicked: a failure here is a clean `failed`, never a held slot.
            validation_error = self._validate_auto(driver)
            if validation_error is not None:
                return self._skip(BookingStatus.FAILED, slot, "validate", started,
                                  validation_error)

        booking_id = self.store.reserve_booking_attempt(slot, mode, started)
        if booking_id is None:
            return self._skip(BookingStatus.SKIPPED_DUPLICATE, slot, "claim", started,
                              "another attempt is already in flight")

        hold_possible = False
        stage = "open"
        try:
            await driver.open_booking_page(self.profile["booking_url"])
            stage = "waf"
            await driver.wait_for_waf()
            stage = "locate"
            if not await driver.locate(slot):
                return await self._finish(booking_id, BookingStatus.SLOT_GONE, slot, "locate",
                                          started, error="slot not offered any more")

            if self.cfg.app.dry_run or self.cfg.booking.dry_run:
                return await self._finish(booking_id, BookingStatus.DRY_RUN, slot, "locate", started,
                                          driver=driver,
                                          error="dry run: reserve not clicked")

            stage = "reserve"
            # From here on the slot may be held even if the call raises: the
            # click can land and a later command in the same step can time out.
            # Every failure past this point is UNKNOWN, never a clean failure.
            hold_possible = True
            await driver.reserve(slot)

            if mode == "assist":
                return await self._hand_off(booking_id, slot, driver, started)

            stage = "applicant"
            applicant = self._load_applicant()
            if not applicant:
                # The slot is already held. Hand it to a human and sound the
                # alarm — reporting a clean failure here would let polling
                # resume and navigate away from a held appointment. Normally
                # unreachable: _validate_auto checks the file before reserving,
                # this stays as the safety net if it vanishes mid-attempt.
                return await self._hand_off(
                    booking_id, slot, driver, started,
                    error="auto mode has no applicant data at data/applicants/<slug>.json "
                          "— finish the form by hand",
                )
            await driver.fill_applicant(applicant)

            stage = "submit"
            await driver.submit()
            ref = await driver.confirmation_ref()
            if not ref:
                return await self._finish(
                    booking_id, BookingStatus.ABORTED_UNKNOWN_STATE, slot, "submit", started,
                    driver=driver,
                    error="submitted but no confirmation marker found — verify by hand",
                )
            self.store.mark_booked(slot)
            return await self._finish(booking_id, BookingStatus.BOOKED, slot, "submit", started,
                                      driver=driver, confirmation_ref=ref)

        except SlotNotFound as exc:
            # Raised before any click: the slot simply is not there any more.
            return await self._finish(booking_id, BookingStatus.SLOT_GONE, slot, "reserve", started,
                                      error=str(exc))
        except SiteNotConfigured as exc:
            status = (
                BookingStatus.ABORTED_UNKNOWN_STATE if hold_possible else BookingStatus.FAILED
            )
            return await self._finish(booking_id, status, slot, stage, started, driver=driver,
                                      error=str(exc))
        except Exception as exc:
            status = BookingStatus.ABORTED_UNKNOWN_STATE if hold_possible else BookingStatus.FAILED
            logger.exception("booking failed for %s at stage %s", self.profile["name"], stage)
            return await self._finish(booking_id, status, slot, stage, started,
                                      driver=driver, error=f"{type(exc).__name__}: {exc}")

    # ── stages ───────────────────────────────────────────────────────────

    async def _hand_off(
        self,
        booking_id: int,
        slot: dict,
        driver: SiteDriver,
        started: str,
        error: str | None = None,
    ) -> BookingResult:
        """The slot is held: wake a human, leave the window exactly as it is."""
        message = (
            f"SLOT CLAIMED — {slot.get('category')} @ {slot.get('location')} "
            f"{slot.get('date')} {slot.get('time') or ''} — finish the form in the browser NOW"
        )
        if error:
            message = f"{message} ({error})"
        self.alarm.start(message)
        if self.dashboard is not None:
            self.dashboard.set_booking(
                self.profile["name"], status=BookingStatus.NEEDS_HUMAN.value,
                stage="assist", slot=slot, started_at=started,
            )
        return await self._finish(booking_id, BookingStatus.NEEDS_HUMAN, slot, "assist", started,
                                  driver=driver, error=error)

    # ── guard ────────────────────────────────────────────────────────────

    def _guard(self, slot: dict, started: str, mode: str) -> BookingResult | None:
        name = slot.get("profile", "")
        if not self.cfg.booking_enabled_for(name):
            return self._skip(BookingStatus.DISABLED, slot, "guard", started,
                              "auto-booking is off for this profile")

        if slot.get("category") != self.profile.get("category") or \
                slot.get("location") != self.profile.get("location"):
            # never book a visa type or VAC the profile did not ask for
            return self._skip(BookingStatus.FAILED, slot, "guard", started,
                              "slot does not match the profile's category/location")

        if self.store.has_booked(name):
            return self._skip(BookingStatus.SKIPPED_DUPLICATE, slot, "guard", started,
                              "this profile already has a booked appointment")

        if self.store.booking_claimed_for_slot(slot):
            return self._skip(BookingStatus.SKIPPED_DUPLICATE, slot, "guard", started,
                              "this slot was already claimed earlier")

        open_row = self.store.open_booking(name)
        if open_row:
            return self._skip(BookingStatus.SKIPPED_DUPLICATE, slot, "guard", started,
                              f"attempt {open_row['id']} is still in flight")

        # A finished needs_human/aborted_unknown_state attempt means a slot may
        # still be held for this profile. The block lives in the DB, so a
        # restart does not quietly un-hold it — a human must resolve the row
        # (Store.resolve_booking) before this profile books anything else.
        unresolved = self.store.unresolved_booking(name)
        if unresolved:
            return self._skip(
                BookingStatus.SKIPPED_DUPLICATE, slot, "guard", started,
                f"attempt {unresolved['id']} ended {unresolved['status']} and is "
                "unresolved — a slot may still be held; resolve it by hand first",
            )

        window_start = iso_pkt(now_pkt() - timedelta(hours=1))
        attempts = self.store.booking_attempts_since(window_start, profile=name)
        if attempts >= self.cfg.booking.max_attempts_per_profile_per_hour:
            return self._skip(BookingStatus.RATE_LIMITED, slot, "guard", started,
                              f"{attempts} attempts in the last hour (cap "
                              f"{self.cfg.booking.max_attempts_per_profile_per_hour})")
        return None

    # ── helpers ──────────────────────────────────────────────────────────

    def _validate_auto(self, driver: SiteDriver) -> str | None:
        """Everything `auto` mode needs, checked BEFORE anything is clicked.

        Returns an error string when unattended booking cannot safely proceed.
        A failure found here is a clean `failed` — the slot is untouched, so
        polling may continue normally.
        """
        selectors = getattr(driver, "selectors", None)
        if selectors is None:
            # drivers that do not carry their selectors are checked against the
            # same file the crawler loads for PlaywrightSiteDriver
            selectors = load_selectors(self.cfg.booking.selectors_file)
        missing = [
            name
            for name, value in (
                ("applicant_fields", selectors.applicant_fields),
                ("submit_button", selectors.submit_button),
                ("confirmation_marker", selectors.confirmation_marker),
            )
            if not value
        ]
        if missing:
            return (
                "auto mode needs selectors " + ", ".join(missing)
                + " — run recon.py and fill recon/booking-selectors.json"
            )
        if not self._load_applicant():
            return "auto mode needs applicant data at data/applicants/<slug>.json"
        return None

    def _load_applicant(self) -> dict:
        applicant_file = self.profile.get("applicant_file")
        if not applicant_file:
            return {}
        path = self.cfg.data_dir / "applicants" / f"{applicant_file}.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            logger.warning("could not read applicant file %s: %s", path, exc)
            return {}

    async def _capture_evidence(self, driver: SiteDriver, slot: dict, started: str) -> str | None:
        if driver is None:
            return None
        try:
            stamp = started.replace(":", "").replace("-", "")[:15]
            base = self.cfg.booking.evidence_dir / f"{stamp}-{slugify(self.profile['name'])}"
            await driver.screenshot(base.with_suffix(".png"))
            try:
                base.with_suffix(".html").write_text(await driver.content())
            except Exception:
                pass
            return str(base.with_suffix(".png"))
        except Exception as exc:
            logger.warning("could not capture booking evidence: %s", exc)
            return None

    async def _finish(
        self,
        booking_id: int,
        status: BookingStatus,
        slot: dict,
        stage: str,
        started: str,
        driver: SiteDriver | None = None,
        confirmation_ref: str | None = None,
        error: str | None = None,
    ) -> BookingResult:
        evidence = await self._capture_evidence(driver, slot, started) if driver else None
        finished = iso_pkt(now_pkt())
        self.store.finish_booking(
            booking_id,
            status.value,
            stage=stage,
            confirmation_ref=confirmation_ref,
            evidence_path=evidence,
            error=error,
            finished_at=finished,
        )

        result = BookingResult(
            status=status.value,
            profile=self.profile["name"],
            slot=dict(slot),
            stage=stage,
            mode=self.cfg.booking.mode,
            confirmation_ref=confirmation_ref,
            evidence_path=evidence,
            error=error,
            booking_id=booking_id,
            started_at=started,
            finished_at=finished,
        )
        self._report(result, error)
        return result

    def _skip(self, status: BookingStatus, slot: dict, stage: str, started: str, error: str | None) -> BookingResult:
        result = BookingResult(
            status=status.value,
            profile=self.profile["name"],
            slot=dict(slot),
            stage=stage,
            mode=self.cfg.booking.mode,
            error=error,
            started_at=started,
            finished_at=iso_pkt(now_pkt()),
        )
        if status not in (BookingStatus.DISABLED,):
            self._report(result, error)
        return result

    def _report(self, result: BookingResult, error: str | None) -> None:
        log = logger.warning if result.status in (
            BookingStatus.FAILED.value,
            BookingStatus.ABORTED_UNKNOWN_STATE.value,
        ) else logger.info
        log(
            "BOOK | profile=%s | status=%s | stage=%s | slot=%s %s | id=%s%s",
            result.profile,
            result.status,
            result.stage,
            result.slot.get("date"),
            result.slot.get("time") or "",
            result.booking_id,
            f" | {error}" if error else "",
        )
        if self.dashboard is not None:
            try:
                self.dashboard.set_booking(
                    result.profile,
                    status=result.status,
                    stage=result.stage,
                    slot=result.slot,
                    confirmation_ref=result.confirmation_ref,
                    evidence_path=result.evidence_path,
                    error=error,
                    finished_at=result.finished_at,
                )
                self.dashboard.push_booking(result.as_dict())
            except Exception:
                logger.debug("dashboard push failed", exc_info=True)
        if self.notifier is not None and result.status != BookingStatus.DISABLED.value:
            try:
                self.notifier.notify_booking(result.as_dict())
            except Exception:
                logger.debug("booking notification failed", exc_info=True)

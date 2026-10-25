"""Regression tests for the reservation-state bugs found in review.

The dangerous class of bug here: a slot that may already be held being reported
as a clean failure. That lets polling resume, navigate away from the reserved
slot, and lose an appointment the bot actually won.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.booking import BookingAgent, BookingStatus
from app.config import AppConfig, BookingConfig, Config, NotificationsConfig, ProfileConfig
from app.store import Store

PROFILE = "Ahmed - Work Permit"


def _cfg(tmp_path: Path, mode: str = "assist", dry_run: bool = False) -> Config:
    return Config(
        app=AppConfig(dry_run=False, headless=True),
        profiles=[
            ProfileConfig(
                name=PROFILE,
                email="a@example.com",
                category="national_d_employment",
                location="lahore",
                booking_url="https://example.test/appointments",
                auto_book=True,
            )
        ],
        notifications=NotificationsConfig(to=["a@example.com"]),
        booking=BookingConfig(
            mode=mode,
            dry_run=dry_run,
            alarm_seconds=1,
            evidence_dir=tmp_path / "evidence",
            profile_dir=tmp_path / "profile",
            selectors_file=tmp_path / "none.json",
        ),
        root=tmp_path,
        data_dir=tmp_path,
        state_dir=tmp_path,
    )


def _profile() -> dict:
    return {
        "name": PROFILE,
        "email": "a@example.com",
        "category": "national_d_employment",
        "location": "lahore",
        "booking_url": "https://example.test/appointments",
        "auto_book": True,
        "prefer": {},
    }


def _slot(date: str = "2026-12-01", time: str = "09:00") -> dict:
    return {
        "profile": PROFILE,
        "location": "lahore",
        "category": "national_d_employment",
        "date": date,
        "time": time,
        "raw": f"{date} {time}",
    }


class _Alarm:
    def __init__(self) -> None:
        self.starts = 0
        self.message: str | None = None

    def start(self, message: str) -> None:
        self.starts += 1
        self.message = message

    def stop(self) -> None:
        pass


class FakeDriver:
    """Drives the agent without a browser; can fail at a chosen point."""

    def __init__(self, fail_in_reserve: bool = False, locate_ok: bool = True, selectors=None):
        self.fail_in_reserve = fail_in_reserve
        self.locate_ok = locate_ok
        self.selectors = selectors
        self.reserved = False
        self.filled: dict | None = None

    async def open_booking_page(self, url: str) -> None: ...
    async def wait_for_waf(self) -> None: ...
    async def locate(self, slot: dict) -> bool:
        return self.locate_ok

    async def reserve(self, slot: dict) -> None:
        self.reserved = True
        if self.fail_in_reserve:
            raise TimeoutError("the click landed, then the follow-up command timed out")

    async def read_tokens(self) -> dict:
        return {}

    async def fill_applicant(self, data: dict) -> None:
        self.filled = data

    async def submit(self) -> None: ...

    async def confirmation_ref(self) -> str | None:
        return "REF-1"

    async def screenshot(self, path: Path) -> None: ...

    async def content(self) -> str:
        return "<html></html>"


def _agent(tmp_path: Path, **kwargs):
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    agent = BookingAgent(_cfg(tmp_path, **kwargs), _profile(), store, alarm=_Alarm())
    return agent, store


def test_failure_after_the_click_is_never_a_clean_failure(tmp_path):
    """Click lands, then a follow-up command times out: the slot may be held."""
    agent, store = _agent(tmp_path)
    result = asyncio.run(agent.book(_slot(), FakeDriver(fail_in_reserve=True)))

    assert result.status == BookingStatus.ABORTED_UNKNOWN_STATE.value
    assert result.claimed is True
    assert store.recent_bookings()[0]["status"] == "aborted_unknown_state"
    store.close()


def test_missing_applicant_data_fails_before_any_click(tmp_path):
    """auto mode validates its prerequisites before reserving: a missing
    applicant file is a clean `failed`, never a held slot."""
    from app.site import BookingSelectors

    agent, store = _agent(tmp_path, mode="auto")
    driver = FakeDriver(
        selectors=BookingSelectors(
            applicant_fields={"passport": "#edit-passport"},
            submit_button="#edit-submit",
            confirmation_marker="#confirmation",
        )
    )

    result = asyncio.run(agent.book(_slot(), driver))

    assert result.status == BookingStatus.FAILED.value
    assert result.stage == "validate"
    assert driver.reserved is False
    assert "applicant" in (result.error or "")
    assert agent.alarm.starts == 0
    assert store.recent_bookings() == []
    store.close()


def test_missing_applicant_after_the_click_still_hands_off(tmp_path, monkeypatch):
    """If the applicant file vanishes between validation and filling, the slot
    is already held — the agent must still alarm a human, not report failure."""
    from app.site import BookingSelectors

    agent, store = _agent(tmp_path, mode="auto")
    calls = []
    original = agent._load_applicant

    def flaky():
        calls.append(1)
        return original() if len(calls) == 1 else {}

    monkeypatch.setattr(agent, "_load_applicant", flaky)
    agent.profile["applicant_file"] = "x"
    driver = FakeDriver(
        selectors=BookingSelectors(
            applicant_fields={"passport": "#edit-passport"},
            submit_button="#edit-submit",
            confirmation_marker="#confirmation",
        )
    )
    (tmp_path / "applicants").mkdir()
    (tmp_path / "applicants" / "x.json").write_text('{"passport": "P1"}')

    result = asyncio.run(agent.book(_slot(), driver))

    assert result.status == BookingStatus.NEEDS_HUMAN.value
    assert driver.reserved is True
    assert "applicant" in (result.error or "")
    assert agent.alarm.starts == 1
    store.close()


def test_slot_gone_before_any_click_stays_a_clean_outcome(tmp_path):
    agent, store = _agent(tmp_path)
    driver = FakeDriver(locate_ok=False)

    result = asyncio.run(agent.book(_slot(), driver))

    assert result.status == BookingStatus.SLOT_GONE.value
    assert driver.reserved is False
    store.close()


def test_rate_cap_counts_only_the_current_profile(tmp_path):
    """A busy profile must not use up another profile's hourly budget."""
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    for i in range(3):
        slot = {
            "profile": "profile-A", "location": "lahore", "category": "c",
            "date": f"2026-12-0{i + 1}", "time": None,
        }
        booking_id = store.reserve_booking_attempt(slot, "assist", f"2026-12-01T00:0{i}:00")
        store.finish_booking(booking_id, "slot_gone", finished_at=f"2026-12-01T00:0{i}:30")

    assert store.booking_attempts_since("2026-12-01T00:00:00", profile="profile-A") == 3
    assert store.booking_attempts_since("2026-12-01T00:00:00", profile="profile-B") == 0
    store.close()

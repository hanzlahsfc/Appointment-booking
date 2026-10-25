"""Tests for booking guards and worker lifecycle added in the safety pass.

Covers: pre-reserve validation for auto mode, restart-safe blocking on
unresolved claims, per-profile rate caps, and the worker scheduler in
main.py (starvation, failure isolation, orderly shutdown).
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from app.booking import BookingAgent, BookingStatus
from app.config import AppConfig, BookingConfig, Config, NotificationsConfig, ProfileConfig
from app.site import BookingSelectors
from app.store import Store
from app.util import iso_pkt, now_pkt

PROFILE = "Ahmed - Work Permit"

AUTO_SELECTORS = BookingSelectors(
    vac_select="select[name='vac']",
    category_select="select[name='category']",
    slot_row=".slot-row",
    reserve_button="button.reserve",
    applicant_fields={"passport": "#edit-passport"},
    submit_button="#edit-submit",
    confirmation_marker="#confirmation",
)


def _cfg(tmp_path: Path, mode: str = "assist", extra_profiles: list | None = None) -> Config:
    profiles = [
        ProfileConfig(
            name=PROFILE,
            email="a@example.com",
            category="national_d_employment",
            location="lahore",
            booking_url="https://example.test/appointments",
            auto_book=True,
        )
    ]
    for name in extra_profiles or []:
        profiles.append(
            ProfileConfig(
                name=name,
                email="b@example.com",
                category="national_d_employment",
                location="lahore",
                booking_url="https://example.test/appointments",
                auto_book=True,
            )
        )
    return Config(
        app=AppConfig(dry_run=False, headless=True),
        profiles=profiles,
        notifications=NotificationsConfig(to=["a@example.com"]),
        booking=BookingConfig(
            mode=mode,
            dry_run=False,
            alarm_seconds=1,
            evidence_dir=tmp_path / "evidence",
            profile_dir=tmp_path / "profile",
            selectors_file=tmp_path / "none.json",
        ),
        root=tmp_path,
        data_dir=tmp_path,
        state_dir=tmp_path,
    )


def _profile(name: str = PROFILE, applicant_file: str | None = None) -> dict:
    return {
        "name": name,
        "email": "a@example.com",
        "category": "national_d_employment",
        "location": "lahore",
        "booking_url": "https://example.test/appointments",
        "auto_book": True,
        "applicant_file": applicant_file,
        "prefer": {},
    }


def _slot(date: str = "2026-12-01", time: str = "09:00", profile: str = PROFILE) -> dict:
    return {
        "profile": profile,
        "location": "lahore",
        "category": "national_d_employment",
        "date": date,
        "time": time,
        "raw": f"{date} {time}",
    }


class _Alarm:
    def __init__(self) -> None:
        self.starts = 0

    def start(self, message: str) -> None:
        self.starts += 1

    def stop(self) -> None:
        pass


class _Driver:
    """SiteDriver double that records which stages ran and can fail on cue."""

    def __init__(self, selectors=AUTO_SELECTORS):
        self.selectors = selectors
        self.calls: list[str] = []
        self.failures: dict[str, Exception] = {}
        self.locate_result = True
        self.confirmation = "REF-1"

    async def _fail(self, stage: str) -> None:
        exc = self.failures.get(stage)
        if exc is not None:
            raise exc

    async def open_booking_page(self, url: str) -> None:
        self.calls.append("open")
        await self._fail("open")

    async def wait_for_waf(self) -> None:
        self.calls.append("waf")
        await self._fail("waf")

    async def locate(self, slot: dict) -> bool:
        self.calls.append("locate")
        await self._fail("locate")
        return self.locate_result

    async def reserve(self, slot: dict) -> None:
        self.calls.append("reserve")
        await self._fail("reserve")

    async def read_tokens(self) -> dict:
        return {}

    async def fill_applicant(self, data: dict) -> None:
        self.calls.append("fill")
        await self._fail("fill")

    async def submit(self) -> None:
        self.calls.append("submit")
        await self._fail("submit")

    async def confirmation_ref(self) -> str | None:
        self.calls.append("confirm")
        await self._fail("confirm")
        return self.confirmation

    async def screenshot(self, path: Path) -> None:
        self.calls.append("screenshot")

    async def content(self) -> str:
        return "<html></html>"


def _agent(cfg: Config, profile: dict, store: Store) -> BookingAgent:
    return BookingAgent(cfg, profile, store, alarm=_Alarm())


def _book(agent: BookingAgent, slot: dict, driver: _Driver):
    return asyncio.run(agent.book(slot, driver))


# ── pre-reserve validation (auto mode) ────────────────────────────────────


def test_auto_requires_submit_selector_before_reserving(tmp_path):
    cfg = _cfg(tmp_path, mode="auto")
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    agent = _agent(cfg, _profile(applicant_file="x"), store)
    (tmp_path / "applicants").mkdir()
    (tmp_path / "applicants" / "x.json").write_text('{"passport": "P1"}')
    driver = _Driver(
        selectors=BookingSelectors(
            vac_select="v", category_select="c", slot_row=".row",
            reserve_button=".res", applicant_fields={"passport": "#p"},
            confirmation_marker="#conf",  # submit_button missing
        )
    )

    result = _book(agent, _slot(), driver)

    assert result.status == BookingStatus.FAILED.value
    assert result.stage == "validate"
    assert "submit_button" in (result.error or "")
    assert "reserve" not in driver.calls and "open" not in driver.calls
    assert store.recent_bookings() == []
    store.close()


def test_auto_without_applicant_never_opens_the_page(tmp_path):
    cfg = _cfg(tmp_path, mode="auto")
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    agent = _agent(cfg, _profile(), store)  # no applicant_file
    driver = _Driver()

    result = _book(agent, _slot(), driver)

    assert result.status == BookingStatus.FAILED.value
    assert result.stage == "validate"
    assert driver.calls == []
    store.close()


def test_assist_mode_skips_auto_validation(tmp_path):
    """Assist needs no applicant data — a human fills the form."""
    cfg = _cfg(tmp_path, mode="assist")
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    agent = _agent(cfg, _profile(), store)
    driver = _Driver()

    result = _book(agent, _slot(), driver)

    assert result.status == BookingStatus.NEEDS_HUMAN.value
    assert "reserve" in driver.calls
    store.close()


# ── unresolved claims survive restarts ────────────────────────────────────


def _seed_finished(store: Store, profile: str, status: str, date: str = "2026-11-01") -> int:
    booking_id = store.reserve_booking_attempt(
        _slot(date=date, profile=profile), "assist", iso_pkt(now_pkt() - timedelta(hours=2))
    )
    store.finish_booking(booking_id, status, finished_at=iso_pkt(now_pkt() - timedelta(hours=1)))
    return booking_id


def test_unresolved_needs_human_blocks_a_different_slot(tmp_path):
    cfg = _cfg(tmp_path, mode="assist")
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    _seed_finished(store, PROFILE, "needs_human")
    agent = _agent(cfg, _profile(), store)
    driver = _Driver()

    result = _book(agent, _slot(date="2026-12-02"), driver)

    assert result.status == BookingStatus.SKIPPED_DUPLICATE.value
    assert "unresolved" in (result.error or "")
    assert "reserve" not in driver.calls
    store.close()


def test_unresolved_aborted_unknown_state_blocks_too(tmp_path):
    cfg = _cfg(tmp_path, mode="assist")
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    _seed_finished(store, PROFILE, "aborted_unknown_state")
    agent = _agent(cfg, _profile(), store)

    result = _book(agent, _slot(date="2026-12-02"), _Driver())

    assert result.status == BookingStatus.SKIPPED_DUPLICATE.value
    store.close()


def test_resolving_the_attempt_unblocks_booking(tmp_path):
    cfg = _cfg(tmp_path, mode="assist")
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    booking_id = _seed_finished(store, PROFILE, "needs_human")

    assert store.resolve_booking(booking_id) is True
    agent = _agent(cfg, _profile(), store)
    result = _book(agent, _slot(date="2026-12-02"), _Driver())

    assert result.status == BookingStatus.NEEDS_HUMAN.value
    store.close()


def test_resolve_booking_refuses_non_unresolved_rows(tmp_path):
    cfg = _cfg(tmp_path, mode="assist")
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))
    booking_id = _seed_finished(store, PROFILE, "booked")

    assert store.resolve_booking(booking_id) is False
    assert store.recent_bookings()[0]["status"] == "booked"
    store.close()


# ── per-profile rate cap ──────────────────────────────────────────────────


def test_rate_cap_applies_per_profile(tmp_path):
    other = "Second Profile"
    cfg = _cfg(tmp_path, mode="assist", extra_profiles=[other])
    cfg.booking.max_attempts_per_profile_per_hour = 2
    store = Store(tempfile.mktemp(suffix=".db", dir=tmp_path))

    for day in ("2026-11-01", "2026-11-02"):
        bid = store.reserve_booking_attempt(
            _slot(date=day), "assist", iso_pkt(now_pkt() - timedelta(minutes=30))
        )
        store.finish_booking(bid, "slot_gone", finished_at=iso_pkt(now_pkt()))

    capped = _book(_agent(cfg, _profile(), store), _slot(date="2026-12-01"), _Driver())
    assert capped.status == BookingStatus.RATE_LIMITED.value

    free = _book(_agent(cfg, _profile(other), store), _slot(date="2026-12-01", profile=other), _Driver())
    assert free.status == BookingStatus.NEEDS_HUMAN.value
    store.close()


# ── worker lifecycle (main.py) ────────────────────────────────────────────


def _run_workers_cfg(tmp_path: Path, names: list[str], concurrency: int) -> Config:
    cfg = _cfg(tmp_path)
    cfg.app.concurrency = concurrency
    cfg.profiles = [
        ProfileConfig(
            name=n,
            email="a@example.com",
            category="c",
            location="l",
            booking_url="https://example.test",
        )
        for n in names
    ]
    return cfg


def test_workers_beyond_concurrency_run_and_shutdown_is_orderly(tmp_path, monkeypatch):
    """concurrency=1 must not starve workers 2..N; stop ends everyone cleanly."""
    import main as main_module

    events: list[tuple[str, str]] = []

    class _Worker:
        def __init__(self, cfg, profile, store, notifier, dashboard=None, demo=False):
            self.profile = profile
            self.demo = demo

        async def start(self):
            events.append(("start", self.profile["name"]))
            await asyncio.sleep(0)  # let the semaphore actually serialize
            if self.profile["name"] == "bad":
                raise RuntimeError("startup boom")

        async def run(self):
            events.append(("run", self.profile["name"]))
            await asyncio.sleep(3600)

        async def stop(self):
            events.append(("stop", self.profile["name"]))

    monkeypatch.setattr(main_module, "CrawlerWorker", _Worker)
    cfg = _run_workers_cfg(tmp_path, ["a", "bad", "c", "d"], concurrency=1)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            main_module._run_workers(cfg, None, None, None, demo=False, stop=stop)
        )
        await asyncio.sleep(0.5)  # all workers should be running by now
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())

    for name in ("a", "c", "d"):
        assert ("run", name) in events, f"worker {name} never ran"
        assert ("stop", name) in events, f"worker {name} was not cleaned up"
    # the failed worker attempted start but never ran; it must not kill the rest
    assert ("run", "bad") not in events

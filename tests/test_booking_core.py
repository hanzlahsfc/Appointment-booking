"""Tests for the booking foundations: config, selection, preflight, alarm, store."""
from __future__ import annotations

import asyncio
import tempfile

import pytest

from app import alarm as alarm_module
from app.alarm import Alarm, AlarmConfig
from app.config import load_config
from app.diff import select_bookable
from app.preflight import check_session, interpret
from app.store import Store

# The real block shell as returned by the portal (trimmed). Kept verbatim enough
# to exercise every marker the detector looks for.
WAF_BODY = (
    '<html style="height:100%"><head><META NAME="ROBOTS" CONTENT="NOINDEX, NOFOLLOW">'
    '<script type="text/javascript" src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3">'
    "</script></head><body><iframe id=\"main-iframe\" src=\"/_Incapsula_Resource?CWUDNSAI=23"
    "&xinfo=54-111018198-0%200NNN%20RT%281792957970941%20151%29&incident_id=7744000080523191925"
    "&edet=17&cinfo=04000000&cip=182.181.248.173\">Request unsuccessful. Incapsula incident ID: "
    "7744000080523191925-843160178734924342</iframe></body></html>"
)


def mk(date, time="09:30", category="national_d_employment", location="lahore"):
    return {
        "profile": "P",
        "location": location,
        "category": category,
        "date": date,
        "time": time,
        "raw": f"{date} {time}",
    }


# ── config ───────────────────────────────────────────────────────────────

def _write_cfg(tmp_path, booking_yaml: str, profile_extra: str = "") -> str:
    path = tmp_path / "config.yaml"
    # booking settings are nested under `app:`, so indent the supplied block
    booking = "\n".join(
        ("  " + line if line.strip() else line) for line in booking_yaml.splitlines()
    )
    path.write_text(
        """
app:
  poll_interval_seconds: 300
  headless: true
{booking}
profiles:
  - name: "Ahmed - Work Permit"
    email: "a@example.com"
    category: "national_d_employment"
    location: "lahore"
    booking_url: "https://example.test/login"
{profile_extra}
notifications:
  to: ["client@example.com"]
""".format(booking=booking, profile_extra=profile_extra)
    )
    return str(path)


def test_booking_defaults_to_off(tmp_path):
    cfg = load_config(_write_cfg(tmp_path, ""))
    assert cfg.booking.mode == "off"
    assert cfg.booking.dry_run is True
    assert cfg.booking.enabled is False
    # booking off => the normal headless setting applies
    assert cfg.headless is cfg.app.headless
    assert cfg.booking_enabled_for("Ahmed - Work Permit") is False


def test_assist_mode_requires_per_profile_opt_in(tmp_path):
    cfg = load_config(
        _write_cfg(tmp_path, "booking:\n  mode: assist\n  dry_run: false\n", "    auto_book: true")
    )
    assert cfg.booking.mode == "assist"
    assert cfg.booking_enabled_for("Ahmed - Work Permit") is True
    # assist must force a visible window so the human can take over
    assert cfg.headless is False
    assert cfg.booking.dry_run is False


def test_profile_that_did_not_opt_in_is_not_booked(tmp_path):
    cfg = load_config(_write_cfg(tmp_path, "booking:\n  mode: assist\n"))
    assert cfg.booking_enabled_for("Ahmed - Work Permit") is False


def test_invalid_booking_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_config(_write_cfg(tmp_path, "booking:\n  mode: yolo\n"))


def test_prefer_block_is_parsed(tmp_path):
    cfg = load_config(
        _write_cfg(
            tmp_path,
            "",
            "    prefer:\n      times: ['09:00']\n      latest_date: '2026-12-01'\n",
        )
    )
    profile = cfg.profiles[0]
    assert profile.prefer["times"] == ["09:00"]
    assert profile.prefer["latest_date"] == "2026-12-01"


# ── slot selection ───────────────────────────────────────────────────────

def test_selects_earliest_date():
    slots = [mk("2026-11-05"), mk("2026-11-01"), mk("2026-11-03")]
    assert select_bookable(slots)["date"] == "2026-11-01"


def test_select_returns_none_when_empty():
    assert select_bookable([]) is None


def test_date_window_filters_candidates():
    slots = [mk("2026-10-01"), mk("2026-12-01")]
    picked = select_bookable(slots, {"earliest_date": "2026-11-01", "latest_date": "2026-12-31"})
    assert picked["date"] == "2026-12-01"


def test_nothing_in_window_returns_none():
    slots = [mk("2026-10-01")]
    assert select_bookable(slots, {"earliest_date": "2026-11-01"}) is None


def test_configured_times_win_over_earlier_date():
    slots = [mk("2026-11-01", "14:30"), mk("2026-11-08", "09:00")]
    picked = select_bookable(slots, {"times": ["09:00", "09:30"]})
    assert picked["time"] == "09:00"


def test_timed_slot_preferred_over_any_time():
    slots = [mk("2026-11-01", None), mk("2026-11-04", "11:00")]
    picked = select_bookable(slots)
    assert picked["time"] == "11:00"


# ── preflight ────────────────────────────────────────────────────────────

def test_redirect_to_login_is_reachable():
    result = interpret(302, "")
    assert result.ok and not result.blocked and result.status_code == 302


def test_403_with_waf_shell_is_reported_as_blocked():
    result = interpret(403, WAF_BODY)
    assert result.blocked is True
    assert result.reachable is False
    assert "clearance" in result.detail
    assert result.snippet


def test_200_carrying_the_waf_shell_is_still_blocked():
    result = interpret(200, WAF_BODY)
    assert result.blocked is True
    assert "WAF shell" in result.detail


def test_server_error_is_not_called_a_block():
    result = interpret(500, "boom")
    assert result.ok is False
    assert result.blocked is False


def test_no_response_is_reported_clearly():
    result = interpret(None, None)
    assert result.ok is False
    assert "no response" in result.detail


class _FakeLocator:
    def __init__(self, count: int):
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, url: str, password_fields: int):
        self.url = url
        self._password_fields = password_fields

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self._password_fields)


def test_session_check_detects_logged_out_page():
    page = _FakePage("https://x.test/login/appointments/en", 1)
    assert asyncio.run(check_session(page)) is False


def test_session_check_accepts_signed_in_page():
    page = _FakePage("https://x.test/en/account", 0)
    assert asyncio.run(check_session(page)) is True


# ── alarm ────────────────────────────────────────────────────────────────

class _SilentBeep:
    calls = 0

    @staticmethod
    def Beep(hz: int, ms: int) -> None:  # noqa: N802 - mirrors winsound
        _SilentBeep.calls += 1


def test_alarm_starts_and_reports_its_message(monkeypatch):
    monkeypatch.setattr(alarm_module, "winsound", _SilentBeep)
    alarm = Alarm(AlarmConfig(seconds=5, gap_seconds=0.01))
    try:
        alarm.start("SLOT CLAIMED — lahore 2026-11-01 09:00")
        assert alarm.active is True
        assert "SLOT CLAIMED" in (alarm.message or "")
    finally:
        alarm.stop()
    assert alarm.active is False


def test_alarm_expires_on_its_own(monkeypatch):
    monkeypatch.setattr(alarm_module, "winsound", _SilentBeep)
    alarm = Alarm(AlarmConfig(seconds=0, gap_seconds=0.01))
    alarm.start("done")
    alarm._thread.join(timeout=2)  # type: ignore[union-attr]
    assert alarm.active is False


def test_alarm_stop_is_idempotent(monkeypatch):
    monkeypatch.setattr(alarm_module, "winsound", _SilentBeep)
    alarm = Alarm(AlarmConfig(seconds=1, gap_seconds=0.01))
    alarm.stop()
    assert alarm.active is False


# ── store: booking log ───────────────────────────────────────────────────

@pytest.fixture
def store():
    s = Store(tempfile.mktemp(suffix=".db"))
    yield s
    s.close()


def test_first_claim_succeeds_second_is_refused(store):
    slot = mk("2026-11-01")
    first = store.reserve_booking_attempt(slot, "assist", "t1")
    assert isinstance(first, int)
    # a second in-flight attempt for the same profile must be impossible
    assert store.reserve_booking_attempt(slot, "assist", "t1") is None


def test_finish_frees_the_profile_for_a_new_attempt(store):
    slot = mk("2026-11-01")
    first = store.reserve_booking_attempt(slot, "assist", "t1")
    store.finish_booking(first, "slot_gone", stage="locate", finished_at="t1")
    second = store.reserve_booking_attempt(mk("2026-11-02"), "assist", "t2")
    assert isinstance(second, int)


def test_open_booking_is_visible(store):
    slot = mk("2026-11-01")
    booking_id = store.reserve_booking_attempt(slot, "assist", "t1")
    open_row = store.open_booking("P")
    assert open_row is not None
    assert open_row["id"] == booking_id
    store.finish_booking(booking_id, "needs_human", finished_at="t1")
    assert store.open_booking("P") is None


def test_has_booked_only_after_a_successful_booking(store):
    slot = mk("2026-11-01")
    booking_id = store.reserve_booking_attempt(slot, "auto", "t1")
    assert store.has_booked("P") is False
    store.finish_booking(booking_id, "needs_human", finished_at="t1")
    assert store.has_booked("P") is False
    other = mk("2026-11-02")
    other_id = store.reserve_booking_attempt(other, "auto", "t2")
    store.finish_booking(other_id, "booked", confirmation_ref="ABC123", finished_at="t2")
    assert store.has_booked("P") is True


def test_attempts_since_counts_for_rate_capping(store):
    for i, day in enumerate(("2026-11-01", "2026-11-02", "2026-11-03")):
        booking_id = store.reserve_booking_attempt(mk(day), "assist", f"2026-11-0{i + 1}T00:00:00")
        store.finish_booking(booking_id, "slot_gone", finished_at=f"2026-11-0{i + 1}T00:01:00")
    assert store.booking_attempts_since("2026-11-02T00:00:00") == 2
    assert len(store.recent_bookings()) == 3


def test_finish_records_the_confirmation_and_stage(store):
    slot = mk("2026-11-01")
    booking_id = store.reserve_booking_attempt(slot, "auto", "t1")
    store.finish_booking(
        booking_id, "booked", stage="submit", confirmation_ref="REF-9", evidence_path="e.png",
        finished_at="t2",
    )
    row = store.recent_bookings()[0]
    assert row["status"] == "booked"
    assert row["confirmation_ref"] == "REF-9"
    assert row["stage"] == "submit"
    assert row["evidence_path"] == "e.png"

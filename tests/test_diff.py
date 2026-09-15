"""Unit tests for slot diffing logic."""
from __future__ import annotations

from app.diff import derive_new_and_alertable, key_of


def mk(profile="Ahmed - Lahore", location="lahore", category="schengen_c", date="2026-05-12", time="09:30", raw="2026-05-12 09:30"):
    return {
        "profile": profile,
        "location": location,
        "category": category,
        "date": date,
        "time": time,
        "raw": raw,
    }


def test_same_slots_no_alert():
    s = mk()
    existing = {key_of(s)}
    assert derive_new_and_alertable([s], existing) == []


def test_single_new_slot_alerts():
    known = mk(date="2026-05-13")
    new = mk(date="2026-05-14", time="10:00", raw="2026-05-14 10:00")
    result = derive_new_and_alertable([known, new], {key_of(known)})
    assert len(result) == 1
    assert result[0]["date"] == "2026-05-14"


def test_slot_removed_no_alert():
    gone = mk(date="2026-05-15")
    still = mk(date="2026-05-16")
    result = derive_new_and_alertable([still], {key_of(gone), key_of(still)})
    assert result == []


def test_returns_slot_with_null_time():
    anytime = mk(time=None, raw="2026-05-17")
    result = derive_new_and_alertable([anytime], set())
    assert len(result) == 1
    assert result[0]["time"] is None


def test_slot_reported_twice_not_duplicated():
    s = mk(date="2026-05-18")
    # Second poll with same slot, already recorded.
    result = derive_new_and_alertable([s], {key_of(s)})
    assert result == []
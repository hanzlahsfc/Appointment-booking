"""Integration tests covering store/diff acceptance clauses."""
from __future__ import annotations

import tempfile

import pytest

from app.store import Store


@pytest.fixture
def store():
    s = Store(tempfile.mktemp(suffix=".db"))
    yield s
    s.close()


def mk(date, time="09:30", profile="P", location="islamabad", category="national_d"):
    return {
        "profile": profile,
        "location": location,
        "category": category,
        "date": date,
        "time": time,
        "raw": f"{date} {time}",
    }


def poll(store, slots, t):
    scope = ("P", "islamabad", "national_d")
    new = store.upsert_slots(slots, t)
    store.deactivate_missing(scope[0], scope[1], scope[2], {(x["date"], x["time"]) for x in slots}, t)
    return new


def test_same_slots_no_alert(store):
    a = mk("2026-05-12")
    assert len(poll(store, [a], "t1")) == 1
    assert poll(store, [a], "t2") == []


def test_single_new_slot_alerts(store):
    a = mk("2026-05-12")
    b = mk("2026-05-13", time=None)
    assert len(poll(store, [a], "t1")) == 1
    new = poll(store, [a, b], "t2")
    assert len(new) == 1
    assert new[0]["date"] == "2026-05-13"


def test_slot_removed_no_alert(store):
    a = mk("2026-05-12")
    poll(store, [a], "t1")
    assert poll(store, [], "t2") == []


def test_readd_unalerted_slot_alerts(store):
    a = mk("2026-05-12")
    poll(store, [a], "t1")
    poll(store, [], "t2")
    assert len(poll(store, [a], "t3")) == 1


def test_readd_alerted_slot_no_alert(store):
    a = mk("2026-05-12")
    poll(store, [a], "t1")
    store.mark_alerted([a])
    poll(store, [], "t2")
    assert poll(store, [a], "t3") == []
    assert poll(store, [a], "t4") == []  # restart: no duplicates


def test_restart_known_slots_no_duplicates(store):
    a = mk("2026-05-12")
    new = poll(store, [a], "t1")
    store.mark_alerted(new)
    store.close()
    s2 = Store(store._path)
    try:
        assert poll(s2, [a], "t2") == []
    finally:
        s2.close()
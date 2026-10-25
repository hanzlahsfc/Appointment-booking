"""Hosted layer unit tests: accounts, sessions, ownership scoping, origin
guard — no browsers needed.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.hosted.auth import RateLimiter
from app.hosted.manager import origin_allowed
from app.hosted.presets import mock_preset
from app.hosted.store import HostedStore, hash_password, verify_password
from app.store import Store
from app.util import iso_pkt, now_pkt


@pytest.fixture()
def hstore(tmp_path: Path):
    store = HostedStore(tmp_path / "hosted.db", tmp_path / "slots.db")
    yield store
    store.close()


def _client_kwargs(**over):
    fields = dict(
        username="alice",
        display_name="Alice A",
        password="correct horse battery",
        profile_name="client:alice",
        booking_url="http://127.0.0.1:8099/en/appointments",
        category="national_d_employment",
        location="lahore",
        allowed_origins=["http://127.0.0.1:8099"],
        selectors={"slot_row": ".slot-row"},
    )
    fields.update(over)
    return fields


# ── accounts ─────────────────────────────────────────────────────────────


def test_add_and_get_client(hstore):
    client = hstore.add_client(**_client_kwargs())
    assert client["id"] >= 1
    again = hstore.client_by_username("alice")
    assert again["profile_name"] == "client:alice"
    assert again["allowed_origins"] == ["http://127.0.0.1:8099"]
    assert "password_hash" not in client  # never leaves the store


def test_duplicate_username_rejected(hstore):
    hstore.add_client(**_client_kwargs())
    with pytest.raises(Exception):
        hstore.add_client(**_client_kwargs())


def test_password_hashing_roundtrip():
    stored = hash_password("hunter2 hunte")
    assert verify_password("hunter2 hunte", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("hunter2 hunte", "garbage")


def test_authenticate_and_password_reset(hstore):
    hstore.add_client(**_client_kwargs())
    assert hstore.authenticate("alice", "correct horse battery") is not None
    assert hstore.authenticate("alice", "nope") is None
    assert hstore.authenticate("nobody", "nope") is None

    client = hstore.client_by_username("alice")
    assert hstore.set_password(client["id"], "new password ok")
    assert hstore.authenticate("alice", "correct horse battery") is None
    assert hstore.authenticate("alice", "new password ok") is not None


def test_deactivate_revokes_sessions_and_blocks_login(hstore):
    hstore.add_client(**_client_kwargs())
    client = hstore.client_by_username("alice")
    token = hstore.create_session(client["id"], 12)
    assert hstore.resolve_session(token) is not None
    hstore.set_active(client["id"], False)
    assert hstore.resolve_session(token) is None
    assert hstore.authenticate("alice", "correct horse battery") is None


# ── sessions ─────────────────────────────────────────────────────────────


def test_session_lifecycle(hstore):
    hstore.add_client(**_client_kwargs())
    client = hstore.client_by_username("alice")
    token = hstore.create_session(client["id"], 12)
    resolved = hstore.resolve_session(token)
    assert resolved["username"] == "alice"
    hstore.revoke_session(token)
    assert hstore.resolve_session(token) is None
    assert hstore.resolve_session("bogus-token") is None


def test_expired_sessions_do_not_resolve(hstore):
    hstore.add_client(**_client_kwargs())
    client = hstore.client_by_username("alice")
    # ttl of 0 hours → expires_at == now; resolution must refuse it
    token = hstore.create_session(client["id"], 0)
    time.sleep(0.01)
    # expires_at <= now: str compare at second granularity — force expiry by
    # rewriting the row into the past
    hstore._conn.execute(
        "UPDATE sessions SET expires_at='2000-01-01T00:00:00Z'"
    )
    hstore._conn.commit()
    assert hstore.resolve_session(token) is None


# ── records scoping (milestone 9) ────────────────────────────────────────


def test_records_are_scoped_to_the_client_profile(tmp_path):
    slot_db = tmp_path / "slots.db"
    store = Store(slot_db)
    now = iso_pkt(now_pkt())
    for name, day in (("client:alice", "2026-12-01"), ("client:bob", "2026-12-02")):
        store.upsert_slots(
            [{
                "profile": name, "location": "lahore",
                "category": "national_d_employment", "date": day,
                "time": "09:00", "raw": day,
            }],
            seen_at=now,
        )
    store.reserve_booking_attempt(
        {"profile": "client:alice", "location": "lahore",
         "category": "national_d_employment", "date": "2026-12-01", "time": "09:00"},
        mode="assist", started_at=now,
    )
    store.close()

    hs = HostedStore(tmp_path / "hosted.db", slot_db)
    alice = hs.records_for("client:alice")
    bob = hs.records_for("client:bob")
    hs.close()

    assert [s["date"] for s in alice["slots"]] == ["2026-12-01"]
    assert len(alice["bookings"]) == 1
    assert [s["date"] for s in bob["slots"]] == ["2026-12-02"]
    assert bob["bookings"] == []


# ── origin guard ─────────────────────────────────────────────────────────


def test_origin_allowed():
    allowed = ["https://portal.example.com", "http://127.0.0.1:8099"]
    assert origin_allowed("https://portal.example.com/en/appointments", allowed)
    assert origin_allowed("http://127.0.0.1:8099/login", allowed)
    assert origin_allowed("about:blank", allowed)
    assert not origin_allowed("https://evil.example.com/", allowed)
    assert not origin_allowed("https://portal.example.com.evil.com/x", allowed)
    assert not origin_allowed("file:///C:/Windows/win.ini", allowed)
    assert not origin_allowed("data:text/html,<h1>x</h1>", allowed)
    assert not origin_allowed("javascript:alert(1)", allowed)
    # different port on the same host is a different origin
    assert not origin_allowed("http://127.0.0.1:9999/en/appointments", allowed)


# ── presets + rate limiter ───────────────────────────────────────────────


def test_mock_preset_shape():
    fields = mock_preset("http://127.0.0.1:8099", "alice")
    assert fields["booking_url"].endswith("/en/appointments")
    assert fields["allowed_origins"] == ["http://127.0.0.1:8099"]
    assert fields["selectors"]["slot_row"] == ".slot-row"
    assert fields["auto_book"] is True


def test_rate_limiter():
    limiter = RateLimiter(max_failures=2, window_seconds=60)
    assert limiter.allow("k")
    limiter.fail("k")
    assert limiter.allow("k")
    limiter.fail("k")
    assert not limiter.allow("k")
    limiter.clear("k")
    assert limiter.allow("k")

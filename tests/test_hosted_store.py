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
from app.hosted.store import (
    HostedStore,
    generate_access_code,
    hash_password,
    verify_password,
)
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
        profile_name="client:alice",
        booking_url="http://127.0.0.1:8099/en/appointments",
        category="national_d_employment",
        location="lahore",
        allowed_origins=["http://127.0.0.1:8099"],
        selectors={"slot_row": ".slot-row"},
    )
    fields.update(over)
    return fields


# ── accounts + access codes ──────────────────────────────────────────────


def test_add_and_get_client(hstore):
    client, code = hstore.add_client(**_client_kwargs())
    assert client["id"] >= 1
    assert code.startswith("ab-") and len(code) > 20
    again = hstore.client_by_username("alice")
    assert again["profile_name"] == "client:alice"
    assert again["allowed_origins"] == ["http://127.0.0.1:8099"]
    # credential hashes never leave the store
    assert "password_hash" not in client
    assert "access_code_hash" not in client


def test_duplicate_username_rejected(hstore):
    hstore.add_client(**_client_kwargs())
    with pytest.raises(Exception):
        hstore.add_client(**_client_kwargs())


def test_access_code_auth(hstore):
    _, code = hstore.add_client(**_client_kwargs())
    assert hstore.authenticate_code(code) is not None
    assert hstore.authenticate_code(code)["username"] == "alice"
    assert hstore.authenticate_code("ab-0000-0000-0000-0000-0000-0000") is None
    assert hstore.authenticate_code("") is None
    assert hstore.authenticate_code("garbage") is None


def test_code_rotation_revokes_old_code_and_sessions(hstore):
    client, code = hstore.add_client(**_client_kwargs())
    token = hstore.create_session(client["id"], 12)
    assert hstore.resolve_session(token) is not None

    new_code = hstore.issue_code(client["id"])
    assert new_code != code
    # old code dies, sessions die, new code works
    assert hstore.authenticate_code(code) is None
    assert hstore.resolve_session(token) is None
    assert hstore.authenticate_code(new_code)["username"] == "alice"


def test_deactivate_blocks_code_login(hstore):
    client, code = hstore.add_client(**_client_kwargs())
    token = hstore.create_session(client["id"], 12)
    hstore.set_active(client["id"], False)
    assert hstore.resolve_session(token) is None
    assert hstore.authenticate_code(code) is None


def test_generated_codes_are_unique_and_unguessable():
    codes = {generate_access_code() for _ in range(100)}
    assert len(codes) == 100


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


def test_complete_booking_marks_booked_and_blocks_further_claims(tmp_path):
    store = Store(tmp_path / "slots.db")
    now = iso_pkt(now_pkt())
    slot = {"profile": "client:alice", "location": "lahore",
            "category": "national_d_employment", "date": "2026-12-01",
            "time": "09:00"}
    bid = store.reserve_booking_attempt(slot, "assist", now)
    store.finish_booking(bid, "needs_human", finished_at=now)

    assert store.complete_booking(bid, "GVCW-ABC123", iso_pkt(now_pkt()))
    assert store.has_booked("client:alice")
    row = [r for r in store.recent_bookings() if r["id"] == bid][0]
    assert row["status"] == "booked"
    assert row["confirmation_ref"] == "GVCW-ABC123"
    # booked is not unresolved — monitoring may resume
    assert store.unresolved_booking("client:alice") is None
    # a still-in-flight attempt must NOT auto-complete
    bid2 = store.reserve_booking_attempt(
        {**slot, "date": "2026-12-05"}, "assist", now)
    assert not store.complete_booking(bid2, "X", now)
    store.close()


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

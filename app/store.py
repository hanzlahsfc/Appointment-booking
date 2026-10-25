"""SQLite storage: slot state + alert log."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_state (
    profile    TEXT NOT NULL,
    location   TEXT NOT NULL,
    category   TEXT NOT NULL,
    date       TEXT NOT NULL,   -- YYYY-MM-DD (PKT)
    time       TEXT,            -- HH:MM or NULL
    raw        TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    alerted    INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (profile, location, category, date, time)
);

CREATE TABLE IF NOT EXISTS alert_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile    TEXT NOT NULL,
    location   TEXT,
    category   TEXT,
    date       TEXT,
    time       TEXT,
    sent_at    TEXT NOT NULL,
    subject    TEXT,
    status     TEXT NOT NULL
);

-- One row per booking attempt. finished_at IS NULL means "in flight"; the
-- partial index below makes a concurrent double-claim impossible in SQLite
-- rather than relying on application-level checks.
CREATE TABLE IF NOT EXISTS booking_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profile          TEXT NOT NULL,
    location         TEXT NOT NULL,
    category         TEXT NOT NULL,
    date             TEXT NOT NULL,
    time             TEXT,
    attempt          INTEGER NOT NULL DEFAULT 1,
    mode             TEXT NOT NULL,
    status           TEXT NOT NULL,
    stage            TEXT,
    confirmation_ref TEXT,
    evidence_path    TEXT,
    error            TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS booking_open_uniq
    ON booking_log(profile) WHERE finished_at IS NULL;
"""


class Store:
    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def upsert_slots(self, slots: Iterable[dict], seen_at: str) -> list[dict]:
        """Refresh slot rows and return slots that warrant an alert.

        A slot warrants an alert when it appears in the current poll and:
          - was never seen before, OR
          - disappeared (active=0) and was never alerted before.
        Slots already alerted never alert again, even if they re-appear.
        """
        conn = self._conn
        new_slots: list[dict] = []
        with self._lock:
            for s in slots:
                key = (s["profile"], s["location"], s["category"], s["date"], s["time"])
                row = conn.execute(
                    "SELECT alerted, active FROM slot_state WHERE profile=? AND location=? AND category=? AND date=? AND time=?",
                    key,
                ).fetchone()
                if row is None:
                    new_slots.append(s)
                    conn.execute(
                        "INSERT INTO slot_state (profile, location, category, date, time, raw, first_seen, last_seen, alerted, active)"
                        " VALUES (?,?,?,?,?,?,?,?,0,1)",
                        (s["profile"], s["location"], s["category"], s["date"], s["time"], s.get("raw"), seen_at, seen_at),
                    )
                else:
                    if not row["active"] and not row["alerted"]:
                        new_slots.append(s)
                    conn.execute(
                        "UPDATE slot_state SET last_seen=?, raw=?, active=1 WHERE profile=? AND location=? AND category=? AND date=? AND time=?",
                        (seen_at, s.get("raw"), *key),
                    )
            conn.commit()
        return new_slots

    def deactivate_missing(
        self,
        profile: str,
        location: str,
        category: str,
        present_keys: set[tuple],
        seen_at: str,
    ) -> None:
        """Mark active rows (for this key scope) absent from the latest poll
        as inactive (slot removed from the site)."""
        conn = self._conn
        with self._lock:
            active_rows = conn.execute(
                "SELECT date, time FROM slot_state "
                "WHERE profile=? AND location=? AND category=? AND active=1",
                (profile, location, category),
            ).fetchall()
            for r in active_rows:
                if (r["date"], r["time"]) not in present_keys:
                    conn.execute(
                        "UPDATE slot_state SET active=0, last_seen=? "
                        "WHERE profile=? AND location=? AND category=? AND date=? AND time=?",
                        (seen_at, profile, location, category, r["date"], r["time"]),
                    )
            conn.commit()

    def mark_alerted(self, new_slots: Iterable[dict]) -> None:
        conn = self._conn
        with self._lock:
            for s in new_slots:
                conn.execute(
                    "UPDATE slot_state SET alerted=1 WHERE profile=? AND location=? AND category=? AND date=? AND time=? AND alerted=0",
                    (s["profile"], s["location"], s["category"], s["date"], s["time"]),
                )
            conn.commit()

    def log_alert(self, slot: dict, sent_at: str, subject: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO alert_log (profile, location, category, date, time, sent_at, subject, status)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (slot["profile"], slot.get("location"), slot.get("category"), slot.get("date"), slot.get("time"), sent_at, subject, status),
            )
            self._conn.commit()

    def recent_alerts(self, since_iso: str) -> list[dict]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM slot_state WHERE first_seen >= ? AND alerted=1 ORDER BY first_seen",
                (since_iso,),
            ).fetchall()
        ]

    # ── booking ──────────────────────────────────────────────────────────

    TERMINAL_STATUSES = ("booked", "failed", "slot_gone", "needs_human", "aborted_unknown_state")

    def reserve_booking_attempt(
        self, slot: dict, mode: str, started_at: str, status: str = "attempted"
    ) -> int | None:
        """Claim the right to book this profile's next slot.

        Returns the new row id, or None when another attempt is already in
        flight (enforced by booking_open_uniq, so it is race-safe).
        """
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO booking_log (profile, location, category, date, time, mode,"
                    " status, stage, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        slot["profile"],
                        slot["location"],
                        slot["category"],
                        slot["date"],
                        slot.get("time"),
                        mode,
                        status,
                        "claim",
                        started_at,
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return None

    def finish_booking(
        self,
        booking_id: int,
        status: str,
        stage: str | None = None,
        confirmation_ref: str | None = None,
        evidence_path: str | None = None,
        error: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE booking_log SET status=?, stage=?, confirmation_ref=?,"
                " evidence_path=?, error=?, finished_at=? WHERE id=?",
                (status, stage, confirmation_ref, evidence_path, error, finished_at, booking_id),
            )
            self._conn.commit()

    def open_booking(self, profile: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM booking_log WHERE profile=? AND finished_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (profile,),
        ).fetchone()
        return dict(row) if row else None

    def has_booked(self, profile: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM booking_log WHERE profile=? AND status='booked' LIMIT 1", (profile,)
        ).fetchone()
        return row is not None

    CLAIMED_STATUSES = ("attempted", "needs_human", "booked", "aborted_unknown_state")

    def booking_claimed_for_slot(self, slot: dict) -> bool:
        """True when this exact slot already produced a claim.

        Prevents re-claiming the same appointment after a hand-off or a failure
        that may have left it held — the one mistake that could double-book a
        client.
        """
        row = self._conn.execute(
            "SELECT 1 FROM booking_log WHERE profile=? AND date=?"
            " AND IFNULL(time,'')=IFNULL(?,'')"
            " AND status IN ('attempted','needs_human','booked','aborted_unknown_state') LIMIT 1",
            (slot["profile"], slot["date"], slot.get("time")),
        ).fetchone()
        return row is not None

    def booking_attempts_since(self, since_iso: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM booking_log WHERE started_at >= ?", (since_iso,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def recent_bookings(self, limit: int = 50) -> list[dict]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM booking_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def mark_booked(self, slot: dict) -> None:
        """Flag the slot row as successfully booked."""
        with self._lock:
            self._conn.execute(
                "UPDATE slot_state SET alerted=1 WHERE profile=? AND location=? AND category=?"
                " AND date=? AND time=?",
                (slot["profile"], slot["location"], slot["category"], slot["date"], slot.get("time")),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
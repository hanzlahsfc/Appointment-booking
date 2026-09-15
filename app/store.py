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

    def close(self) -> None:
        self._conn.close()
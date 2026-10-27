"""Sequential applicant queue for the local Windows app.

One ordered list of applicants, persisted in SQLite so progress survives
restarts. The monitor processes them in `position` order: while a booking
is held or unresolved the queue PARKS — it never advances past an
ambiguous outcome and never reprocesses an identity.

Applicant data is private: names/keys stay in this local DB only — never
committed, never logged raw. Identity dedup hashes (surname+passport)
so a passport number never leaves plaintext anywhere.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS applicants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position    INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    display_name TEXT NOT NULL,
    identity    TEXT NOT NULL UNIQUE,          -- sha256 of surname+passport
    data_json   TEXT NOT NULL DEFAULT '{}',    -- private applicant fields
    category    TEXT,
    date_from   TEXT,                          -- DD/MM/YYYY acceptable range
    date_to     TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    result_ref  TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_applicants_position ON applicants(position, status);
"""

STATUSES = {
    "queued", "monitoring", "selected_awaiting_human",
    "submitting", "booked", "failed", "outcome_unknown",
    "paused",
}
DONE = {"booked", "failed"}


def _identity(data: dict) -> str:
    raw = f"{(data.get('surname') or '').strip().lower()}|" \
          f"{(data.get('passportnumber') or '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


class ApplicantQueue:
    def __init__(self, db_path: str | Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    # ── CRUD ─────────────────────────────────────────────────────────────

    def _row(self, r) -> dict:
        d = dict(r)
        d["data"] = json.loads(d.pop("data_json") or "{}")
        return d

    def list(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM applicants ORDER BY position, id").fetchall()
        return [self._row(r) for r in rows]

    def current(self) -> dict | None:
        """The first applicant not yet done — the one being worked."""
        row = self._conn.execute(
            "SELECT * FROM applicants WHERE status NOT IN ('booked','failed')"
            " ORDER BY position, id LIMIT 1").fetchone()
        return self._row(row) if row else None

    def add(self, data: dict, display_name: str, *,
            category: str | None = None, date_from: str | None = None,
            date_to: str | None = None) -> dict:
        ident = _identity(data)
        with self._lock, self._conn:
            pos = (self._conn.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM applicants")
                .fetchone()[0])
            now = time.time()
            try:
                cur = self._conn.execute(
                    "INSERT INTO applicants (position,display_name,identity,"
                    " data_json,category,date_from,date_to,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (pos, display_name, ident, json.dumps(data),
                     category, date_from, date_to, now, now))
            except sqlite3.IntegrityError:
                raise ValueError("an applicant with the same surname+passport "
                                 "is already in the queue")
            return self._row(self._conn.execute(
                "SELECT * FROM applicants WHERE id=?", (cur.lastrowid,)).fetchone())

    def update(self, app_id: int, *, display_name=None, data=None,
               category=None, date_from=None, date_to=None) -> bool:
        sets, vals = [], []
        if display_name is not None:
            sets.append("display_name=?"); vals.append(display_name)
        if data is not None:
            sets.append("data_json=?"); vals.append(json.dumps(data))
            sets.append("identity=?"); vals.append(_identity(data))
        for col, v in (("category", category), ("date_from", date_from),
                       ("date_to", date_to)):
            if v is not None:
                sets.append(f"{col}=?"); vals.append(v)
        if not sets:
            return False
        sets.append("updated_at=?"); vals.append(time.time())
        vals.append(app_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE applicants SET {', '.join(sets)} WHERE id=?", vals)
            return cur.rowcount > 0

    def remove(self, app_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM applicants WHERE id=? AND status IN ('queued','paused')",
                (app_id,))
            return cur.rowcount > 0

    def move(self, app_id: int, direction: int) -> bool:
        """direction: -1 up, +1 down — swaps positions with the neighbour."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id,position FROM applicants WHERE id=?", (app_id,)).fetchone()
            if not row:
                return False
            cmp = "<" if direction < 0 else ">"
            order = "DESC" if direction < 0 else "ASC"
            other = self._conn.execute(
                f"SELECT id,position FROM applicants WHERE position {cmp} ?"
                f" ORDER BY position {order} LIMIT 1",
                (row["position"],)).fetchone()
            if not other:
                return False
            self._conn.execute(
                "UPDATE applicants SET position=? WHERE id=?",
                (other["position"], row["id"]))
            self._conn.execute(
                "UPDATE applicants SET position=? WHERE id=?",
                (row["position"], other["id"]))
            return True

    # ── processing states ────────────────────────────────────────────────

    def set_status(self, app_id: int, status: str, *,
                   note: str | None = None, result_ref: str | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError(f"bad status {status}")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE applicants SET status=?, note=COALESCE(?,note),"
                " result_ref=COALESCE(?,result_ref), updated_at=? WHERE id=?",
                (status, note, result_ref, time.time(), app_id))
            return cur.rowcount > 0

    def is_done(self) -> bool:
        return self.current() is None

    def close(self) -> None:
        self._conn.close()

"""Hosted-client account store: clients + dashboard sessions.

Lives in its own SQLite file (data/hosted.db) so the account layer stays
self-contained for a later VPS move. Passwords are PBKDF2-HMAC-SHA256; session
tokens are random 256-bit values stored only as SHA-256 hashes, so a leaked
database cannot be replayed into live sessions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL DEFAULT '',
    access_code_hash TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    -- The monitoring profile this client owns. slot_state/booking_log rows are
    -- keyed by profile_name; it is also the client's identity for ownership.
    profile_name    TEXT NOT NULL UNIQUE,
    booking_url     TEXT NOT NULL,
    category        TEXT NOT NULL,
    location        TEXT NOT NULL,
    allowed_origins TEXT NOT NULL,           -- JSON list of scheme://host[:port]
    selectors       TEXT NOT NULL DEFAULT '{}',
    auto_book       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    client_id  INTEGER NOT NULL REFERENCES clients(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
"""

PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, salt: str | None = None,
                  iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
    )
    return f"pbkdf2${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, expected = stored.split("$", 3)
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_access_code() -> str:
    """A single unguessable dashboard credential: 96 bits of entropy, grouped
    for reading (ab-xxxx-xxxx-xxxx-xxxx-xxxx-xxxx). Shown to the operator once
    at issue time, stored only as a PBKDF2 hash — the database alone cannot be
    replayed into a login."""
    return "ab-" + "-".join(secrets.token_hex(2) for _ in range(6))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class HostedStore:
    """Clients and sessions. Reads of per-client monitoring data (slots,
    bookings) go through a second, read-only connection to the crawler DB so
    this layer never writes outside its own tables."""

    def __init__(self, db_path: str | Path, slot_db_path: str | Path | None = None):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # Idempotent upgrade for DBs created before access codes existed.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(clients)")}
        if "access_code_hash" not in cols:
            self._conn.execute(
                "ALTER TABLE clients ADD COLUMN access_code_hash TEXT NOT NULL DEFAULT ''"
            )
        self._conn.commit()
        self._slot_db = str(slot_db_path) if slot_db_path else None
        self._slot_conn: sqlite3.Connection | None = None

    # ── clients ──────────────────────────────────────────────────────────

    def add_client(
        self,
        username: str,
        display_name: str,
        profile_name: str,
        booking_url: str,
        category: str,
        location: str,
        allowed_origins: list[str],
        selectors: dict[str, Any] | None = None,
        auto_book: bool = False,
        access_code: str | None = None,
    ) -> tuple[dict, str]:
        """Create a client; returns (client, plaintext access code).

        The code is shown to the operator exactly once — only its PBKDF2 hash
        is stored. There is no API to read it back.
        """
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        code = access_code or generate_access_code()
        origins = [o.rstrip("/") for o in allowed_origins]
        if not origins:
            raise ValueError("at least one allowed origin is required")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO clients (password_hash, username, display_name, access_code_hash,"
                " created_at, profile_name, booking_url, category, location,"
                " allowed_origins, selectors, auto_book) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "",  # Legacy databases have NOT NULL without a default.
                    username,
                    display_name.strip() or username,
                    hash_password(code),
                    _now(),
                    profile_name,
                    booking_url,
                    category,
                    location,
                    json.dumps(origins),
                    json.dumps(selectors or {}),
                    1 if auto_book else 0,
                ),
            )
            self._conn.commit()
            return self.get_client(int(cur.lastrowid)), code  # type: ignore[return-value]

    def list_clients(self) -> list[dict]:
        return [self._row_to_client(r) for r in self._conn.execute(
            "SELECT * FROM clients ORDER BY id"
        )]

    def get_client(self, client_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM clients WHERE id=?", (client_id,)
        ).fetchone()
        return self._row_to_client(row) if row else None

    def client_by_username(self, username: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM clients WHERE username=?", (username.strip(),)
        ).fetchone()
        return self._row_to_client(row) if row else None

    def issue_code(self, client_id: int) -> str | None:
        """Rotate the access code; returns the new plaintext code once.

        Rotating revokes every open session — a leaked code stops working the
        moment a new one is issued.
        """
        code = generate_access_code()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE clients SET access_code_hash=? WHERE id=?",
                (hash_password(code), client_id),
            )
            self._conn.execute("DELETE FROM sessions WHERE client_id=?", (client_id,))
            self._conn.commit()
        return code if cur.rowcount > 0 else None

    def set_active(self, client_id: int, active: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE clients SET active=? WHERE id=?", (1 if active else 0, client_id)
            )
            if not active:
                self._conn.execute("DELETE FROM sessions WHERE client_id=?", (client_id,))
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_client(row: sqlite3.Row) -> dict:
        client = dict(row)
        # Hashed credentials never leave the store, even internally.
        client.pop("password_hash", None)
        client.pop("access_code_hash", None)
        client["allowed_origins"] = json.loads(client.get("allowed_origins") or "[]")
        client["selectors"] = json.loads(client.get("selectors") or "{}")
        client["active"] = bool(client["active"])
        client["auto_book"] = bool(client["auto_book"])
        return client

    # ── login + sessions ─────────────────────────────────────────────────

    def authenticate_code(self, code: str) -> dict | None:
        """Code -> client row, or None.

        There is no username: the code itself identifies the client. Every
        active row is verified (PBKDF2 is deliberately slow), so the timing
        does not reveal whether a prefix matched or how many clients exist.
        """
        if not code or not code.strip():
            return None
        code = code.strip()
        rows = self._conn.execute(
            "SELECT * FROM clients WHERE active=1 AND access_code_hash != ''"
        ).fetchall()
        match = None
        for row in rows:
            if verify_password(code, row["access_code_hash"]):
                match = row
        if match is None:
            return None
        return self._row_to_client(match)

    def create_session(self, client_id: int, ttl_hours: int) -> str:
        token = secrets.token_urlsafe(32)
        expires = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl_hours * 3600)
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, client_id, created_at, expires_at,"
                " last_seen) VALUES (?,?,?,?,?)",
                (_hash_token(token), client_id, _now(), expires, _now()),
            )
            self._conn.commit()
        return token

    def resolve_session(self, token: str) -> dict | None:
        """Token -> client row, or None. Sliding last_seen; hard expiry."""
        if not token:
            return None
        row = self._conn.execute(
            "SELECT s.client_id AS cid, s.expires_at FROM sessions s WHERE s.token_hash=?",
            (_hash_token(token),),
        ).fetchone()
        if row is None or row["expires_at"] <= _now():
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET last_seen=? WHERE token_hash=?",
                (_now(), _hash_token(token)),
            )
            self._conn.commit()
        client = self.get_client(int(row["cid"]))
        if client is None or not client["active"]:
            return None
        return client

    def revoke_session(self, token: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),)
            )
            self._conn.commit()

    def purge_expired_sessions(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (_now(),)
            )
            self._conn.commit()
            return cur.rowcount

    # ── per-client monitoring records (read-only, crawler DB) ────────────

    def _slots_conn(self) -> sqlite3.Connection | None:
        if self._slot_db is None or not Path(self._slot_db).exists():
            return None
        if self._slot_conn is None:
            self._slot_conn = sqlite3.connect(
                f"file:{self._slot_db}?mode=ro", uri=True, check_same_thread=False
            )
            self._slot_conn.row_factory = sqlite3.Row
        return self._slot_conn

    def records_for(self, profile_name: str, limit: int = 200) -> dict:
        """Everything the dashboard shows a client, scoped to their profile.

        The profile filter is applied in SQL — there is no code path that
        returns another profile's rows to a client.
        """
        conn = self._slots_conn()
        if conn is None:
            return {"slots": [], "alerts": [], "bookings": []}
        slots = [
            dict(r)
            for r in conn.execute(
                "SELECT location, category, date, time, first_seen, last_seen, active"
                " FROM slot_state WHERE profile=? ORDER BY date, time LIMIT ?",
                (profile_name, limit),
            )
        ]
        alerts = [
            dict(r)
            for r in conn.execute(
                "SELECT date, time, category, location, sent_at, subject, status"
                " FROM alert_log WHERE profile=? ORDER BY id DESC LIMIT ?",
                (profile_name, 50),
            )
        ]
        bookings = [
            dict(r)
            for r in conn.execute(
                "SELECT id, status, stage, date, time, confirmation_ref, error,"
                " started_at, finished_at FROM booking_log WHERE profile=?"
                " ORDER BY id DESC LIMIT ?",
                (profile_name, 50),
            )
        ]
        return {"slots": slots, "alerts": alerts, "bookings": bookings}

    def close(self) -> None:
        self._conn.close()
        if self._slot_conn is not None:
            self._slot_conn.close()

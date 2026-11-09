"""DialUp's durable state, in ``<hermes home>/plugin-data/dialup/data.db`` (SQLite, WAL).

Tables:
  replies(phone, created_at, text)         last few agent replies per number, for the USSD reader
  sms_log(phone, ts) / turn_log(phone, ts)  rolling-hour rate caps (they survive restarts)
  notices(phone, kind, ts)                 "limit reached" SMS already sent this hour
  status(phone, text, ts)                  the latest busy/status line, shown on the USSD home screen
  pending_approvals(...)                   commands waiting for the user's OK (P1)

All methods are synchronous and small. The adapter calls them through ``asyncio.to_thread`` so a
slow disk or a WAL checkpoint never blocks the event loop that also answers USSD callbacks.
A lock serialises access because one connection is shared across those worker threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

PLUGIN_NAME = "dialup"
REPLIES_KEPT = 5
HOUR = 3600
STATUS_FRESH_SECONDS = 15 * 60
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS replies (phone TEXT NOT NULL, created_at REAL NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS replies_phone ON replies (phone, created_at);
CREATE TABLE IF NOT EXISTS sms_log (phone TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sms_log_phone ON sms_log (phone, ts);
CREATE TABLE IF NOT EXISTS turn_log (phone TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS turn_log_phone ON turn_log (phone, ts);
CREATE TABLE IF NOT EXISTS notices (phone TEXT NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS notices_phone ON notices (phone, kind, ts);
CREATE TABLE IF NOT EXISTS status (phone TEXT PRIMARY KEY, text TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending_approvals (
    request_id TEXT PRIMARY KEY, phone TEXT NOT NULL, session_key TEXT NOT NULL,
    text TEXT NOT NULL, actions_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS pending_phone ON pending_approvals (phone, created_at);
"""


@dataclass(frozen=True)
class Pending:
    request_id: str
    phone: str
    session_key: str
    text: str
    choices: tuple[str, ...]
    created_at: float


@dataclass(frozen=True)
class Snapshot:
    """What one USSD callback needs, read in one go."""

    last_reply: Optional[str]
    status_line: Optional[str]
    pending: Optional[Pending]


class Store:
    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        if conn is None:
            from plugins.plugin_storage import plugin_db
            conn = plugin_db(PLUGIN_NAME)
        self._conn = conn
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- replies ---------------------------------------------------------------------------

    def add_reply(self, phone: str, text: str, *, ttl_hours: int, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO replies (phone, created_at, text) VALUES (?, ?, ?)", (phone, now, text))
            # Keep the newest few per number, and nothing past its retention.
            self._conn.execute(
                "DELETE FROM replies WHERE phone = ? AND rowid NOT IN "
                "(SELECT rowid FROM replies WHERE phone = ? ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (phone, phone, REPLIES_KEPT))
            self._conn.execute("DELETE FROM replies WHERE created_at < ?", (now - ttl_hours * HOUR,))

    def last_reply(self, phone: str, *, ttl_hours: int, now: Optional[float] = None) -> Optional[str]:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM replies WHERE phone = ? AND created_at >= ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (phone, now - ttl_hours * HOUR)).fetchone()
        return row[0] if row else None

    # -- status line -----------------------------------------------------------------------

    def set_status(self, phone: str, text: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO status (phone, text, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(phone) DO UPDATE SET text = excluded.text, ts = excluded.ts", (phone, text, now))

    def clear_status(self, phone: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM status WHERE phone = ?", (phone,))

    # -- rate caps ---------------------------------------------------------------------------

    def _try_consume(self, table: str, phone: str, cap: int, now: float) -> bool:
        with self._lock, self._conn:
            self._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (now - HOUR,))
            (count,) = self._conn.execute(f"SELECT COUNT(*) FROM {table} WHERE phone = ?", (phone,)).fetchone()
            if count >= cap:
                return False
            self._conn.execute(f"INSERT INTO {table} (phone, ts) VALUES (?, ?)", (phone, now))
            return True

    def try_consume_sms(self, phone: str, cap: int, now: Optional[float] = None) -> bool:
        """Count one SMS against the hourly cap; False (and nothing counted) when it's reached."""
        return self._try_consume("sms_log", phone, cap, time.time() if now is None else now)

    def try_consume_turn(self, phone: str, cap: int, now: Optional[float] = None) -> bool:
        """Count one agent turn against the hourly cap; False when it's reached."""
        return self._try_consume("turn_log", phone, cap, time.time() if now is None else now)

    def claim_notice(self, phone: str, kind: str, now: Optional[float] = None) -> bool:
        """True once per number and kind per hour: whether to send a one-off notice now."""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM notices WHERE ts < ?", (now - HOUR,))
            row = self._conn.execute(
                "SELECT 1 FROM notices WHERE phone = ? AND kind = ? LIMIT 1", (phone, kind)).fetchone()
            if row:
                return False
            self._conn.execute("INSERT INTO notices (phone, kind, ts) VALUES (?, ?, ?)", (phone, kind, now))
            return True

    def counts_last_hour(self, now: Optional[float] = None) -> dict[str, tuple[int, int]]:
        """``{phone: (sms, turns)}`` over the last hour, for ``hermes dialup status``."""
        now = time.time() if now is None else now
        out: dict[str, list[int]] = {}
        with self._lock:
            for index, table in enumerate(("sms_log", "turn_log")):
                for phone, count in self._conn.execute(
                        f"SELECT phone, COUNT(*) FROM {table} WHERE ts >= ? GROUP BY phone", (now - HOUR,)):
                    out.setdefault(phone, [0, 0])[index] = count
        return {phone: (c[0], c[1]) for phone, c in out.items()}

    # -- pending approvals (P1) --------------------------------------------------------------

    def add_pending(self, pending: Pending) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_approvals "
                "(request_id, phone, session_key, text, actions_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (pending.request_id, pending.phone, pending.session_key, pending.text,
                 json.dumps(list(pending.choices)), pending.created_at))

    def oldest_pending(self, phone: str, *, max_age: float, now: Optional[float] = None) -> Optional[Pending]:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pending_approvals WHERE created_at < ?", (now - max_age,))
            row = self._conn.execute(
                "SELECT request_id, phone, session_key, text, actions_json, created_at FROM pending_approvals "
                "WHERE phone = ? ORDER BY created_at ASC LIMIT 1", (phone,)).fetchone()
        if row is None:
            return None
        return Pending(row[0], row[1], row[2], row[3], tuple(json.loads(row[4])), row[5])

    def get_pending(self, request_id: str) -> Optional[Pending]:
        with self._lock:
            row = self._conn.execute(
                "SELECT request_id, phone, session_key, text, actions_json, created_at FROM pending_approvals "
                "WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        return Pending(row[0], row[1], row[2], row[3], tuple(json.loads(row[4])), row[5])

    def remove_pending(self, request_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pending_approvals WHERE request_id = ?", (request_id,))

    # -- one read per USSD callback ------------------------------------------------------------

    def snapshot(self, phone: str, *, ttl_hours: int, approval_max_age: float,
                 now: Optional[float] = None) -> Snapshot:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT text, ts FROM status WHERE phone = ?", (phone,)).fetchone()
        status = row[0] if row and now - row[1] <= STATUS_FRESH_SECONDS else None
        return Snapshot(
            last_reply=self.last_reply(phone, ttl_hours=ttl_hours, now=now),
            status_line=status,
            pending=self.oldest_pending(phone, max_age=approval_max_age, now=now))

    # -- housekeeping ----------------------------------------------------------------------------

    def purge(self) -> dict[str, int]:
        """Delete everything DialUp stored. Returns rows removed per table."""
        removed = {}
        with self._lock, self._conn:
            for table in ("replies", "sms_log", "turn_log", "notices", "status", "pending_approvals"):
                removed[table] = self._conn.execute(f"DELETE FROM {table}").rowcount
        return removed

"""SQLite access for one storage node.

Each thread gets its own connection; the file runs in WAL mode so readers do
not block the writer. Writes that read before they write run inside one
`BEGIN IMMEDIATE` transaction, which takes the write lock up front and makes
the check-then-write atomic across threads.

Values are opaque strings: stored and returned exactly as received, never
parsed. Versions and the deleted flag live in their own columns.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from .timefmt import now_rfc3339

BUSY_TIMEOUT_S = 5.0

KEY_PATTERN = re.compile(
    r"^(request|resource):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,
    version    INTEGER NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL
)
"""


@dataclass(frozen=True)
class Record:
    key: str
    value: str
    version: int
    deleted: bool
    updated_at: str

    def envelope(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "version": self.version,
            "deleted": self.deleted,
            "updated_at": self.updated_at,
        }


SCAN_PREFIXES = ("request:", "resource:")


class NotFound(Exception):
    """The key does not exist, or holds only a tombstone."""


class VersionConflict(Exception):
    """`if_version` did not match the key's current version."""

    def __init__(self, current_version: int):
        super().__init__(f"current version is {current_version}")
        self.current_version = current_version


def _check_expected(current: int, if_version: Optional[int]) -> None:
    if if_version is not None and if_version != current:
        raise VersionConflict(current)


def _record(row) -> Record:
    key, value, version, deleted, updated_at = row
    return Record(key, value, version, bool(deleted), updated_at)


class Store:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: autocommit, transactions are explicit.
        return sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
        return conn

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def ping(self) -> None:
        self._conn().execute("SELECT 1").fetchone()

    @staticmethod
    def _version_of(conn: sqlite3.Connection, key: str) -> tuple[int, bool]:
        """(current version, deleted); (0, False) for a key never written."""
        row = conn.execute("SELECT version, deleted FROM kv WHERE key = ?", (key,)).fetchone()
        return (row[0], bool(row[1])) if row else (0, False)

    def get(self, key: str) -> Optional[Record]:
        """The stored row, tombstones included; None if the key was never written."""
        row = self._conn().execute(
            "SELECT key, value, version, deleted, updated_at FROM kv WHERE key = ?", (key,)
        ).fetchone()
        return _record(row) if row else None

    def put(self, key: str, value: str, if_version: Optional[int] = None) -> Record:
        """Write as primary: store the value under the next version.

        With `if_version`, write only if the key's current version equals it
        (0 = never written; a tombstone's version counts as current).
        """
        with self._write_tx() as conn:
            current, _ = self._version_of(conn, key)
            _check_expected(current, if_version)
            record = Record(key, value, current + 1, False, now_rfc3339())
            conn.execute(
                "INSERT INTO kv (key, value, version, deleted, updated_at) VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, version = excluded.version, "
                "deleted = 0, updated_at = excluded.updated_at",
                (key, value, record.version, record.updated_at),
            )
        return record

    def delete(self, key: str, if_version: Optional[int] = None) -> Record:
        """Delete as primary: replace the row with a tombstone under the next version."""
        with self._write_tx() as conn:
            current, deleted = self._version_of(conn, key)
            if current == 0 or deleted:
                raise NotFound(key)
            _check_expected(current, if_version)
            record = Record(key, "", current + 1, True, now_rfc3339())
            conn.execute(
                "UPDATE kv SET value = '', version = ?, deleted = 1, updated_at = ? WHERE key = ?",
                (record.version, record.updated_at, key),
            )
        return record

    def apply_replica(self, record: Record) -> tuple[bool, int]:
        """Store a row exactly as the primary wrote it, if it is newer than ours.

        Never bumps the version. Returns (applied, version now held). Getting a
        version we already hold is normal (retries, sync overlapping live
        writes) and is not an error.
        """
        with self._write_tx() as conn:
            current, _ = self._version_of(conn, record.key)
            if record.version <= current:
                return False, current
            conn.execute(
                "INSERT INTO kv (key, value, version, deleted, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, version = excluded.version, "
                "deleted = excluded.deleted, updated_at = excluded.updated_at",
                (record.key, record.value, record.version, int(record.deleted), record.updated_at),
            )
        return True, record.version

    def scan(self, prefix: str) -> list[Record]:
        """Live records whose key starts with `prefix`, ordered by key."""
        if prefix not in SCAN_PREFIXES:
            raise ValueError(f"unsupported prefix {prefix!r}")
        # Range on the primary key instead of LIKE, which ignores case and
        # cannot use the index. ';' is the character after ':'.
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        rows = self._conn().execute(
            "SELECT key, value, version, deleted, updated_at FROM kv "
            "WHERE key >= ? AND key < ? AND deleted = 0 ORDER BY key",
            (prefix, upper),
        ).fetchall()
        return [_record(row) for row in rows]

    def stats(self) -> dict:
        keys, tombstones, max_version = self._conn().execute(
            "SELECT COALESCE(SUM(deleted = 0), 0), COALESCE(SUM(deleted = 1), 0), COALESCE(MAX(version), 0) FROM kv"
        ).fetchone()
        return {"keys": keys, "tombstones": tombstones, "max_version": max_version}

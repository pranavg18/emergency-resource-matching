"""SQLite access for one storage node.

Each thread gets its own connection; the file runs in WAL mode so readers do
not block the writer. Writes that read before they write run inside one
`BEGIN IMMEDIATE` transaction, which takes the write lock up front and makes
the check-then-write atomic across threads.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

BUSY_TIMEOUT_S = 5.0


class Store:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")

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

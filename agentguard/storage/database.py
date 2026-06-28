"""
Thread-local SQLite connection management.

Reference:
- EDS §6.1 — SQLite Storage: Connection Strategy

CRITICAL: A single sqlite3.Connection must never be shared across threads.
This module enforces one connection per thread via threading.local(), with
check_same_thread=True so any accidental cross-thread use fails loudly
instead of corrupting data silently.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from agentguard.exceptions import AgentGuardDatabaseError
from agentguard.storage.schema import ALL_DDL_STATEMENTS, SCHEMA_VERSION


def utcnow_iso() -> str:
    """Canonical UTC timestamp format used everywhere in AgentGuard."""
    return datetime.now(timezone.utc).isoformat()


class DatabaseManager:
    """
    Owns the SQLite database file and hands out thread-local connections.

    One DatabaseManager instance is created per AgentGuard instance and
    shared across all threads that AgentGuard operates on. Internally it
    never shares a raw connection object across threads.
    """

    def __init__(self, db_path: str, busy_timeout_ms: int = 5000):
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it on first access."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(
                    self._db_path,
                    check_same_thread=True,
                    isolation_level=None,  # We manage transactions explicitly.
                )
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
                conn.row_factory = sqlite3.Row
            except sqlite3.Error as exc:
                raise AgentGuardDatabaseError(
                    f"Failed to open SQLite database at {self._db_path!r}: {exc}"
                ) from exc
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """
        Explicit transaction with commit on success, rollback on exception.

        Usage:
            with db.transaction() as conn:
                conn.execute("INSERT INTO ...")
        """
        conn = self._get_connection()
        try:
            conn.execute("BEGIN")
        except sqlite3.OperationalError as exc:
            raise AgentGuardDatabaseError(
                f"Failed to start transaction (likely lock timeout exhausted): {exc}"
            ) from exc
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Convenience: run a single non-transactional read/write statement."""
        conn = self._get_connection()
        try:
            return conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise AgentGuardDatabaseError(f"SQLite error executing query: {exc}") from exc

    def close_thread_connection(self) -> None:
        """Close and discard this thread's connection, if one exists."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Schema setup / migrations
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """
        Eagerly apply DDL and record the schema version.

        Idempotent: safe to call on an existing database. Protected by a
        lock so concurrent DatabaseManager construction (e.g. in tests)
        doesn't race on table creation.
        """
        with self._init_lock:
            if self._initialized:
                return
            conn = self._get_connection()
            try:
                for statement in ALL_DDL_STATEMENTS:
                    conn.execute(statement)
                cur = conn.execute(
                    "SELECT version FROM migrations WHERE version = ?",
                    (SCHEMA_VERSION,),
                )
                if cur.fetchone() is None:
                    conn.execute(
                        "INSERT INTO migrations (version, applied_at) VALUES (?, ?)",
                        (SCHEMA_VERSION, utcnow_iso()),
                    )
            except sqlite3.Error as exc:
                raise AgentGuardDatabaseError(
                    f"Failed to initialize schema at {self._db_path!r}: {exc}"
                ) from exc
            self._initialized = True
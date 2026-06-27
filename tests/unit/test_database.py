"""
Tests for agentguard.storage.database.DatabaseManager.

Covers:
- Schema creation on fresh DB
- Idempotent re-initialization
- Thread-local connection isolation (the critical correctness property)
- Transaction commit/rollback
- Concurrent writes from multiple threads do not corrupt state
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from agentguard.exceptions import AgentGuardDatabaseError
from agentguard.storage.database import DatabaseManager, utcnow_iso


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def db(db_path):
    manager = DatabaseManager(db_path)
    yield manager
    manager.close_thread_connection()


class TestSchemaCreation:
    def test_creates_all_tables(self, db):
        conn = db._get_connection()
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"runs", "approvals", "audit_log", "migrations"} <= tables

    def test_records_schema_version(self, db):
        conn = db._get_connection()
        row = conn.execute("SELECT version FROM migrations").fetchone()
        assert row["version"] == 1

    def test_idempotent_construction(self, db_path):
        # Constructing a second DatabaseManager against the same file
        # must not fail or duplicate migration rows.
        db1 = DatabaseManager(db_path)
        db2 = DatabaseManager(db_path)
        conn = db2._get_connection()
        count = conn.execute("SELECT COUNT(*) AS c FROM migrations").fetchone()["c"]
        assert count == 1
        db1.close_thread_connection()
        db2.close_thread_connection()

    def test_pragmas_applied(self, db):
        conn = db._get_connection()
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "wal"
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert foreign_keys == 1


class TestThreadLocalConnections:
    def test_same_thread_reuses_connection(self, db):
        conn1 = db._get_connection()
        conn2 = db._get_connection()
        assert conn1 is conn2

    def test_different_threads_get_different_connections(self, db):
        """
        Each thread must get its own distinct connection object.

        IMPORTANT: we keep a live reference to each connection (not just
        its id()) until all threads have finished and we've made all
        comparisons. If we only stored id() and let the connection object
        itself go out of scope when its thread exited, CPython's allocator
        is free to reclaim that memory and hand the SAME address to the
        next object created -- which is exactly what id() reuse looks
        like, and is indistinguishable from "these were the same object"
        if you only compare integers. Keeping references alive avoids
        this false-negative entirely, on any platform/allocator.
        """
        connections = {}
        barrier_release = threading.Event()

        def capture(name):
            connections[name] = db._get_connection()
            # Hold this thread alive until the main thread says it's
            # safe to exit, so its connection object cannot be GC'd
            # and its memory address recycled before we've compared it.
            barrier_release.wait(timeout=5)

        t1 = threading.Thread(target=capture, args=("t1",))
        t2 = threading.Thread(target=capture, args=("t2",))
        t1.start()
        t2.start()

        # Wait until both threads have captured their connection.
        import time
        deadline = time.time() + 5
        while ("t1" not in connections or "t2" not in connections) and time.time() < deadline:
            time.sleep(0.01)

        main_conn = db._get_connection()

        # All three connection objects are alive simultaneously right now,
        # so id() comparison (or direct object identity via `is`) is valid.
        assert connections["t1"] is not connections["t2"]
        assert connections["t1"] is not main_conn
        assert connections["t2"] is not main_conn

        barrier_release.set()
        t1.join()
        t2.join()

    def test_concurrent_writes_from_multiple_threads_succeed(self, db):
        """
        Each thread inserts its own run row. If thread-local isolation
        is broken, this either raises sqlite3.ProgrammingError or
        produces fewer than expected rows due to corruption.
        """
        errors = []

        def insert_run(run_id):
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO runs (run_id, created_at, framework) "
                        "VALUES (?, ?, ?)",
                        (run_id, utcnow_iso(), "test"),
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=insert_run, args=(f"run-{i}",))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        conn = db._get_connection()
        count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        assert count == 20


class TestTransactions:
    def test_commit_persists_data(self, db):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, created_at, framework) VALUES (?, ?, ?)",
                ("run-commit", utcnow_iso(), "test"),
            )
        conn = db._get_connection()
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", ("run-commit",)
        ).fetchone()
        assert row is not None

    def test_exception_rolls_back(self, db):
        with pytest.raises(ValueError):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO runs (run_id, created_at, framework) VALUES (?, ?, ?)",
                    ("run-rollback", utcnow_iso(), "test"),
                )
                raise ValueError("simulated failure mid-transaction")

        conn = db._get_connection()
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", ("run-rollback",)
        ).fetchone()
        assert row is None

    def test_composite_unique_constraint_on_approvals(self, db):
        """Verifies the idempotency-critical UNIQUE index actually exists."""
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, created_at, framework) VALUES (?, ?, ?)",
                ("run-x", utcnow_iso(), "test"),
            )
            conn.execute(
                """INSERT INTO approvals
                   (approval_id, run_id, tool_name, arguments_hash, policy_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("appr-1", "run-x", "wire_transfer", "hash123", "1.0.0", utcnow_iso()),
            )

        with pytest.raises(Exception):
            with db.transaction() as conn:
                # Same (run_id, tool_name, arguments_hash, policy_version) -> must fail.
                conn.execute(
                    """INSERT INTO approvals
                       (approval_id, run_id, tool_name, arguments_hash, policy_version, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("appr-2", "run-x", "wire_transfer", "hash123", "1.0.0", utcnow_iso()),
                )


class TestErrorHandling:
    def test_unwritable_path_raises_agentguard_error(self, tmp_path):
        bad_path = str(tmp_path / "nonexistent_dir" / "db.sqlite")
        with pytest.raises(AgentGuardDatabaseError):
            DatabaseManager(bad_path)

    def test_close_thread_connection_is_safe_to_call_twice(self, db):
        db.close_thread_connection()
        db.close_thread_connection()  # must not raise
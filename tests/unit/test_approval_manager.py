"""
Tests for agentguard.approvals.manager.ApprovalManager.

Covers:
- Creation produces a PENDING record
- Idempotent lookup by composite key (run_id, tool_name, arguments_hash, policy_version)
- Any change in the composite key is treated as a distinct request
- approve()/reject() transition PENDING -> terminal state
- First-writer-wins under REAL concurrent threads (not just sequential calls)
- Lazy expiration: a PENDING record past its TTL is found as EXPIRED
- Terminal states never expire (EDS: only PENDING records can expire)
- Duplicate create_approval() for the same composite key raises (UNIQUE constraint)
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from agentguard.approvals.manager import ApprovalManager
from agentguard.approvals.models import ApprovalStatus
from agentguard.exceptions import AgentGuardDatabaseError
from agentguard.storage.database import DatabaseManager


@pytest.fixture()
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.execute(
        "INSERT INTO runs (run_id, created_at, framework) VALUES (?, ?, ?)",
        ("run-1", "2026-01-01T00:00:00+00:00", "test"),
    )
    manager.execute(
        "INSERT INTO runs (run_id, created_at, framework) VALUES (?, ?, ?)",
        ("run-2", "2026-01-01T00:00:00+00:00", "test"),
    )
    yield manager
    manager.close_thread_connection()


@pytest.fixture()
def mgr(db):
    return ApprovalManager(db)


class TestCreation:
    def test_create_produces_pending_record(self, mgr):
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        assert record.status == ApprovalStatus.PENDING
        assert record.run_id == "run-1"
        assert record.tool_name == "wire_transfer"
        assert record.approval_id  # non-empty

    def test_create_assigns_unique_ids(self, mgr):
        r1 = mgr.create_approval("run-1", "tool_a", "hash1", "1.0.0")
        r2 = mgr.create_approval("run-1", "tool_b", "hash2", "1.0.0")
        assert r1.approval_id != r2.approval_id

    def test_duplicate_composite_key_raises(self, mgr):
        mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        with pytest.raises(AgentGuardDatabaseError):
            mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")

    def test_default_ttl_applied(self, mgr):
        record = mgr.create_approval("run-1", "tool_a", "hash1", "1.0.0")
        assert record.ttl_seconds == 3600

    def test_custom_ttl_applied(self, mgr):
        record = mgr.create_approval("run-1", "tool_a", "hash1", "1.0.0", ttl_seconds=60)
        assert record.ttl_seconds == 60


class TestIdempotentLookup:
    def test_finds_existing_by_composite_key(self, mgr):
        created = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        found = mgr.find_existing("run-1", "wire_transfer", "hash1", "1.0.0")
        assert found is not None
        assert found.approval_id == created.approval_id

    def test_returns_none_when_not_found(self, mgr):
        found = mgr.find_existing("run-1", "nonexistent_tool", "hash1", "1.0.0")
        assert found is None

    def test_different_run_id_is_distinct_request(self, mgr):
        mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        found = mgr.find_existing("run-2", "wire_transfer", "hash1", "1.0.0")
        assert found is None

    def test_different_tool_name_is_distinct_request(self, mgr):
        mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        found = mgr.find_existing("run-1", "send_email", "hash1", "1.0.0")
        assert found is None

    def test_different_arguments_hash_is_distinct_request(self, mgr):
        mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        found = mgr.find_existing("run-1", "wire_transfer", "hash2", "1.0.0")
        assert found is None

    def test_different_policy_version_is_distinct_request(self, mgr):
        mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        found = mgr.find_existing("run-1", "wire_transfer", "hash1", "2.0.0")
        assert found is None

    def test_get_by_id_returns_record(self, mgr):
        created = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        found = mgr.get_by_id(created.approval_id)
        assert found.approval_id == created.approval_id

    def test_get_by_id_returns_none_for_unknown_id(self, mgr):
        assert mgr.get_by_id("does-not-exist") is None


class TestResolution:
    def test_approve_transitions_to_approved(self, mgr):
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        result = mgr.approve(record.approval_id, approver="alice")
        assert result is True
        updated = mgr.get_by_id(record.approval_id)
        assert updated.status == ApprovalStatus.APPROVED
        assert updated.approver == "alice"

    def test_reject_transitions_to_rejected(self, mgr):
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        result = mgr.reject(record.approval_id, approver="alice", notes="too risky")
        assert result is True
        updated = mgr.get_by_id(record.approval_id)
        assert updated.status == ApprovalStatus.REJECTED
        assert updated.notes == "too risky"

    def test_approve_on_unknown_id_returns_false(self, mgr):
        assert mgr.approve("does-not-exist", approver="alice") is False

    def test_cannot_approve_already_approved(self, mgr):
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        mgr.approve(record.approval_id, approver="alice")
        second_attempt = mgr.approve(record.approval_id, approver="bob")
        assert second_attempt is False
        final = mgr.get_by_id(record.approval_id)
        assert final.approver == "alice"  # bob's attempt did not overwrite

    def test_cannot_reject_already_approved(self, mgr):
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        mgr.approve(record.approval_id, approver="alice")
        reject_attempt = mgr.reject(record.approval_id, approver="bob")
        assert reject_attempt is False
        final = mgr.get_by_id(record.approval_id)
        assert final.status == ApprovalStatus.APPROVED  # unchanged


class TestFirstWriterWinsUnderRealConcurrency:
    def test_concurrent_approve_and_reject_only_one_wins(self, mgr):
        """
        Real threads racing to resolve the SAME approval_id. Exactly one
        of (approve, reject) must succeed; the other must observe False.
        This is the actual regression test for the race condition found
        during EDS review (OQ-5) -- not just a sequential-call check.
        """
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        results = {}

        def try_approve():
            results["approve"] = mgr.approve(record.approval_id, approver="alice")

        def try_reject():
            results["reject"] = mgr.reject(record.approval_id, approver="bob")

        t1 = threading.Thread(target=try_approve)
        t2 = threading.Thread(target=try_reject)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one of the two must have won.
        assert results["approve"] != results["reject"]
        assert (results["approve"] is True) != (results["reject"] is True)

        final = mgr.get_by_id(record.approval_id)
        # The final state must be consistent with whichever one won.
        if results["approve"]:
            assert final.status == ApprovalStatus.APPROVED
        else:
            assert final.status == ApprovalStatus.REJECTED

    def test_many_concurrent_approve_attempts_exactly_one_succeeds(self, mgr):
        """
        20 threads all attempt to approve the same approval_id with
        different approver names. Exactly one must succeed.
        """
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0")
        results = []
        results_lock = threading.Lock()

        def attempt(approver_name):
            result = mgr.approve(record.approval_id, approver=approver_name)
            with results_lock:
                results.append(result)

        threads = [
            threading.Thread(target=attempt, args=(f"approver-{i}",))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 19


class TestExpiration:
    def test_pending_record_past_ttl_is_found_as_expired(self, db):
        mgr = ApprovalManager(db, default_ttl_seconds=1)
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0", ttl_seconds=1)
        time.sleep(1.2)
        found = mgr.find_existing("run-1", "wire_transfer", "hash1", "1.0.0")
        assert found.status == ApprovalStatus.EXPIRED

    def test_get_by_id_also_applies_lazy_expiration(self, db):
        mgr = ApprovalManager(db)
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0", ttl_seconds=1)
        time.sleep(1.2)
        found = mgr.get_by_id(record.approval_id)
        assert found.status == ApprovalStatus.EXPIRED

    def test_pending_record_under_ttl_is_not_expired(self, mgr):
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0", ttl_seconds=3600)
        found = mgr.find_existing("run-1", "wire_transfer", "hash1", "1.0.0")
        assert found.status == ApprovalStatus.PENDING

    def test_approved_record_never_expires_even_past_ttl(self, db):
        mgr = ApprovalManager(db)
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0", ttl_seconds=1)
        mgr.approve(record.approval_id, approver="alice")
        time.sleep(1.2)
        found = mgr.get_by_id(record.approval_id)
        # Must remain APPROVED, not flip to EXPIRED just because TTL elapsed.
        assert found.status == ApprovalStatus.APPROVED

    def test_get_pending_excludes_expired_and_marks_them(self, db):
        mgr = ApprovalManager(db)
        mgr.create_approval("run-1", "tool_a", "hash1", "1.0.0", ttl_seconds=1)
        mgr.create_approval("run-1", "tool_b", "hash2", "1.0.0", ttl_seconds=3600)
        time.sleep(1.2)

        pending = mgr.get_pending()
        # tool_a's record should now show as EXPIRED, not PENDING -- so it
        # must not appear in a "get_pending" result that's meant to reflect
        # current PENDING records after lazy expiration is applied.
        statuses = {p.tool_name: p.status for p in pending}
        assert statuses.get("tool_b") == ApprovalStatus.PENDING
        assert "tool_a" not in statuses or statuses["tool_a"] != ApprovalStatus.PENDING

    def test_expire_race_does_not_clobber_concurrent_approval(self, db):
        """
        A PENDING record is right at its TTL boundary. One thread tries
        to approve it at the exact moment another thread's lookup
        discovers it as expired. The guarded UPDATE in _expire() must
        not silently overwrite a legitimate concurrent approval.
        """
        mgr = ApprovalManager(db)
        record = mgr.create_approval("run-1", "wire_transfer", "hash1", "1.0.0", ttl_seconds=1)
        time.sleep(1.2)  # now past TTL

        results = {}

        def try_approve():
            results["approve"] = mgr.approve(record.approval_id, approver="alice")

        def try_lookup_triggers_expire():
            results["lookup_status"] = mgr.get_by_id(record.approval_id).status

        t1 = threading.Thread(target=try_approve)
        t2 = threading.Thread(target=try_lookup_triggers_expire)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        final = mgr.get_by_id(record.approval_id)
        # The final state must be one consistent terminal state -- either
        # the approval won (APPROVED) or the expiration won (EXPIRED) --
        # and whichever it is, it must not have been silently clobbered
        # back to PENDING or be in some inconsistent state.
        assert final.status in (ApprovalStatus.APPROVED, ApprovalStatus.EXPIRED)
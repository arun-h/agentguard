"""
Approval Manager: idempotent approval requests, lifecycle state machine,
expiration, and conflict-safe resolution.

Reference: EDS Section 4.3 (Feature 3: Approval Workflow), Section 4.3.4
(Idempotency), Section 4.3.5 (Expiration), OQ-5 (Concurrent Approval
Update Race -- RESOLVED as first-writer-wins).
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from agentguard.approvals.models import ApprovalRecord, ApprovalStatus
from agentguard.exceptions import AgentGuardDatabaseError
from agentguard.storage.database import DatabaseManager, utcnow_iso


def _row_to_record(row) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row["approval_id"],
        run_id=row["run_id"],
        tool_name=row["tool_name"],
        arguments_hash=row["arguments_hash"],
        policy_version=row["policy_version"],
        status=ApprovalStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        ttl_seconds=row["ttl_seconds"],
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        approver=row["approver"],
        notes=row["notes"],
    )


class ApprovalManager:
    """
    Owns approval lifecycle against the `approvals` SQLite table.

    All idempotency, expiration, and conflict-resolution rules are
    enforced here -- this is the only code path permitted to write to
    the approvals table.
    """

    def __init__(self, db: DatabaseManager, default_ttl_seconds: int = 3600):
        self._db = db
        self._default_ttl = default_ttl_seconds

    # ------------------------------------------------------------------
    # Lookup (EDS 4.3.4 - Idempotency)
    # ------------------------------------------------------------------

    def find_existing(
        self,
        run_id: str,
        tool_name: str,
        arguments_hash: str,
        policy_version: str,
    ) -> Optional[ApprovalRecord]:
        """
        Composite-key lookup: (run_id, tool_name, arguments_hash, policy_version).

        A change in ANY of these four fields is a distinct, new approval
        request (EDS 4.3.4). Returns None if no matching record exists.
        Expiration is applied lazily here: a PENDING record whose TTL has
        elapsed is transitioned to EXPIRED as a side effect of being
        looked up, and the EXPIRED record (not the stale PENDING one) is
        what gets returned.
        """
        row = self._db.execute(
            """SELECT * FROM approvals
               WHERE run_id = ? AND tool_name = ?
                 AND arguments_hash = ? AND policy_version = ?""",
            (run_id, tool_name, arguments_hash, policy_version),
        ).fetchone()

        if row is None:
            return None

        record = _row_to_record(row)
        if record.is_expired(datetime.now(timezone.utc)):
            return self._expire(record)
        return record

    def get_by_id(self, approval_id: str) -> Optional[ApprovalRecord]:
        """Return any approval record by ID regardless of status."""
        row = self._db.execute(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        record = _row_to_record(row)
        if record.is_expired(datetime.now(timezone.utc)):
            return self._expire(record)
        return record

    def get_pending(self) -> List[ApprovalRecord]:
        """Return all PENDING approval records across all runs."""
        rows = self._db.execute(
            "SELECT * FROM approvals WHERE status = ?", (ApprovalStatus.PENDING.value,)
        ).fetchall()
        records = [_row_to_record(r) for r in rows]
        now = datetime.now(timezone.utc)
        result = []
        for record in records:
            if record.is_expired(now):
                result.append(self._expire(record))
            else:
                result.append(record)
        return result

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_approval(
        self,
        run_id: str,
        tool_name: str,
        arguments_hash: str,
        policy_version: str,
        ttl_seconds: Optional[int] = None,
    ) -> ApprovalRecord:
        """
        Create a new PENDING approval request.

        Callers should call find_existing() first (EDS 4.3.4 idempotency
        contract) -- this method does NOT check for an existing record
        itself, since the decision of "is this a new request or a resume"
        belongs to the caller, who has the policy-evaluation context.
        Calling this twice for the same composite key will raise
        AgentGuardDatabaseError (the UNIQUE index on the approvals table
        rejects the duplicate insert) rather than silently overwriting.
        """
        approval_id = str(uuid.uuid4())
        now = utcnow_iso()
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl

        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """INSERT INTO approvals
                       (approval_id, run_id, tool_name, arguments_hash,
                        policy_version, status, created_at, ttl_seconds)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        approval_id,
                        run_id,
                        tool_name,
                        arguments_hash,
                        policy_version,
                        ApprovalStatus.PENDING.value,
                        now,
                        ttl,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # The UNIQUE index on (run_id, tool_name, arguments_hash,
            # policy_version) rejected this insert -- an approval for
            # this exact composite key already exists. Callers are
            # expected to call find_existing() before create_approval()
            # to avoid this in normal operation (EDS 4.3.4); this
            # exception is the safety net for callers that skip that
            # check, translated into AgentGuard's own exception type
            # rather than leaking a raw sqlite3 exception.
            raise AgentGuardDatabaseError(
                f"An approval already exists for run_id={run_id!r}, "
                f"tool_name={tool_name!r}, arguments_hash={arguments_hash!r}, "
                f"policy_version={policy_version!r}. Call find_existing() "
                f"before create_approval() to retrieve it instead of "
                f"creating a duplicate."
            ) from exc

        return ApprovalRecord(
            approval_id=approval_id,
            run_id=run_id,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
            policy_version=policy_version,
            status=ApprovalStatus.PENDING,
            created_at=datetime.fromisoformat(now),
            ttl_seconds=ttl,
        )

    # ------------------------------------------------------------------
    # Resolution (EDS 4.3.6, OQ-5 fix: first-writer-wins)
    # ------------------------------------------------------------------

    def approve(self, approval_id: str, approver: str, notes: str = "") -> bool:
        """
        Transition PENDING -> APPROVED.

        Returns True if THIS call performed the transition. Returns False
        if the record was already resolved by another writer (first-writer-
        wins, per OQ-5) -- the caller should re-fetch via get_by_id() to
        see the actual current state in that case.
        """
        return self._resolve(approval_id, ApprovalStatus.APPROVED, approver, notes)

    def reject(self, approval_id: str, approver: str, notes: str = "") -> bool:
        """Transition PENDING -> REJECTED. Same first-writer-wins contract as approve()."""
        return self._resolve(approval_id, ApprovalStatus.REJECTED, approver, notes)

    def _resolve(
        self, approval_id: str, new_status: ApprovalStatus, approver: str, notes: str
    ) -> bool:
        """
        First-writer-wins resolution (EDS OQ-5, resolved).

        Uses an UPDATE ... WHERE status='PENDING' guard clause. If another
        writer already resolved this approval (status is no longer
        PENDING by the time this UPDATE runs), affected row count is 0,
        and this call returns False without overwriting the existing
        terminal state. This is the fix for the race condition originally
        found as a contradiction between "last writer wins" (in the error
        handling matrix) and "first writer wins" (in Open Questions) --
        first-writer-wins is the resolved, correct behavior.
        """
        now = utcnow_iso()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE approvals
                   SET status = ?, updated_at = ?, approver = ?, notes = ?
                   WHERE approval_id = ? AND status = 'PENDING'""",
                (new_status.value, now, approver, notes, approval_id),
            )
            if cursor.rowcount == 0:
                return False
            return True

    def _expire(self, record: ApprovalRecord) -> ApprovalRecord:
        """
        Transition a lazily-detected-expired PENDING record to EXPIRED.

        Uses the same first-writer-wins guard as _resolve(), since another
        thread could be concurrently approving/rejecting this exact record
        at the same moment it's discovered to be past its TTL. If the
        guarded UPDATE affects 0 rows, someone else already resolved it
        first (approved/rejected) -- in that case, re-fetch and return the
        actual current state rather than incorrectly reporting EXPIRED.
        """
        now = utcnow_iso()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE approvals
                   SET status = ?, updated_at = ?
                   WHERE approval_id = ? AND status = 'PENDING'""",
                (ApprovalStatus.EXPIRED.value, now, record.approval_id),
            )
            if cursor.rowcount == 0:
                # Someone else resolved it first; fetch the real current state.
                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?",
                    (record.approval_id,),
                ).fetchone()
                return _row_to_record(row)

        record.status = ApprovalStatus.EXPIRED
        record.updated_at = datetime.fromisoformat(now)
        return record
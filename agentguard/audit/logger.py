"""
Audit Logger: append-only persistence of every governance decision.

Reference:
- EDS §5.4 — Audit Logging
- EDS §5.4.1 — Audit Logging: Immutability Contract
- EDS §5.4.2 — Audit Logging: Query Patterns

IMMUTABILITY CONTRACT: this class deliberately exposes NO update or
delete method anywhere in its public surface. Immutability is enforced
by absence of capability, not by a runtime permission check on an
otherwise-present method -- there is nothing to bypass because there is
nothing to call. If a future change ever adds an update/delete path here,
that is itself a violation of EDS 4.4.3 and should be rejected in review.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import List, Optional

from agentguard.audit.models import AuditRecord
from agentguard.context import ExecutionContext
from agentguard.decisions.models import PolicyDecision
from agentguard.exceptions import AgentGuardDatabaseError
from agentguard.storage.database import DatabaseManager, utcnow_iso


def _row_to_record(row) -> AuditRecord:
    metadata = json.loads(row["metadata"]) if row["metadata"] else None
    return AuditRecord(
        id=row["id"],
        run_id=row["run_id"],
        approval_id=row["approval_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        tool_name=row["tool_name"],
        arguments_hash=row["arguments_hash"],
        policy_version=row["policy_version"],
        decision=row["decision"],
        reason=row["reason"],
        rule_matched=row["rule_matched"],
        budget_calls_used=row["budget_calls_used"],
        budget_cost_used=row["budget_cost_used"],
        loop_count=row["loop_count"],
        agent_id=row["agent_id"],
        framework=row["framework"],
        metadata=metadata,
    )


class AuditLogger:
    """
    Owns all writes to the `audit_log` table. Every governance decision
    -- ALLOW, DENY, or REQUIRE_APPROVAL -- should be logged exactly once
    via log_decision().
    """

    def __init__(self, db: DatabaseManager):
        self._db = db

    # ------------------------------------------------------------------
    # Write (append-only)
    # ------------------------------------------------------------------

    def log_decision(
        self,
        ctx: ExecutionContext,
        decision: PolicyDecision,
        approval_id: Optional[str] = None,
        budget_calls_used: Optional[int] = None,
        budget_cost_used: Optional[float] = None,
        loop_count: Optional[int] = None,
    ) -> AuditRecord:
        """
        Insert one audit_log row for a single governance decision.

        ctx and decision together supply most fields. approval_id,
        budget_calls_used, budget_cost_used, and loop_count are optional
        because most decisions involve none of those subsystems (a plain
        policy ALLOW with no budget/loop check triggered, for instance,
        has none of these set) -- callers pass whichever are relevant to
        the specific decision being logged.

        Reference: EDS Section 4.4.2 for the full column list.
        """
        timestamp = utcnow_iso()
        metadata_json = json.dumps(ctx.metadata) if ctx.metadata else None

        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    """INSERT INTO audit_log
                       (run_id, approval_id, timestamp, tool_name, arguments_hash,
                        policy_version, decision, reason, rule_matched,
                        budget_calls_used, budget_cost_used, loop_count,
                        agent_id, framework, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ctx.run_id,
                        approval_id,
                        timestamp,
                        ctx.tool_name,
                        ctx.arguments_hash,
                        decision.policy_version,
                        decision.decision.value,
                        decision.reason,
                        decision.rule_matched,
                        budget_calls_used,
                        budget_cost_used,
                        loop_count,
                        ctx.agent_id,
                        ctx.framework,
                        metadata_json,
                    ),
                )
                row_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            # Most likely cause: approval_id references a row that does
            # not exist in the approvals table (the audit_log.approval_id
            # column is a foreign key). Translate into AgentGuard's own
            # exception type rather than leaking a raw sqlite3 exception --
            # same pattern as ApprovalManager.create_approval().
            raise AgentGuardDatabaseError(
                f"Failed to write audit record for run_id={ctx.run_id!r}, "
                f"tool_name={ctx.tool_name!r}: {exc}. If approval_id was "
                f"supplied, confirm it references an approval created via "
                f"ApprovalManager.create_approval() first."
            ) from exc

        return AuditRecord(
            id=row_id,
            run_id=ctx.run_id,
            approval_id=approval_id,
            timestamp=datetime.fromisoformat(timestamp),
            tool_name=ctx.tool_name,
            arguments_hash=ctx.arguments_hash,
            policy_version=decision.policy_version,
            decision=decision.decision.value,
            reason=decision.reason,
            rule_matched=decision.rule_matched,
            budget_calls_used=budget_calls_used,
            budget_cost_used=budget_cost_used,
            loop_count=loop_count,
            agent_id=ctx.agent_id,
            framework=ctx.framework,
            metadata=ctx.metadata if ctx.metadata else None,
        )

    # ------------------------------------------------------------------
    # Read (query patterns from EDS 4.4.5)
    # ------------------------------------------------------------------

    def get_by_run_id(self, run_id: str) -> List[AuditRecord]:
        """All events for a given run_id, in insertion order."""
        rows = self._db.execute(
            "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id ASC", (run_id,)
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_by_decision(self, decision: str, since: Optional[str] = None) -> List[AuditRecord]:
        """
        All events matching a specific decision type (e.g. 'DENY'),
        optionally filtered to records at or after an ISO-8601 timestamp.
        """
        if since:
            rows = self._db.execute(
                """SELECT * FROM audit_log
                   WHERE decision = ? AND timestamp >= ?
                   ORDER BY id ASC""",
                (decision, since),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM audit_log WHERE decision = ? ORDER BY id ASC",
                (decision,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_by_tool_name(self, tool_name: str) -> List[AuditRecord]:
        """All events for a given tool_name across all runs."""
        rows = self._db.execute(
            "SELECT * FROM audit_log WHERE tool_name = ? ORDER BY id ASC",
            (tool_name,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_approval_events_for_run(self, run_id: str) -> List[AuditRecord]:
        """All REQUIRE_APPROVAL events for a given run_id."""
        rows = self._db.execute(
            """SELECT * FROM audit_log
               WHERE run_id = ? AND decision = 'REQUIRE_APPROVAL'
               ORDER BY id ASC""",
            (run_id,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def count_by_decision(self) -> dict:
        """Returns {decision_type: count} across the entire audit log."""
        rows = self._db.execute(
            "SELECT decision, COUNT(*) AS c FROM audit_log GROUP BY decision"
        ).fetchall()
        return {row["decision"]: row["c"] for row in rows}
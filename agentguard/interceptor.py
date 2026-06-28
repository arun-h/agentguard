"""
Interceptor: the core tool-call interception logic.

This is the engine behind AgentGuard.tool() / AgentGuard.wrap(). It wires
together every other subsystem (DecisionEngine, ApprovalManager,
AuditLogger) into the single per-call execution sequence defined by the
AgentGuard runtime.

Reference:
- EDS §5.1 — Tool Interception
- EDS §5.3.2 — Approval Workflow: Idempotency
- EDS §5.4 — Audit Logging
- EDS §5.7 — Decision Engine
"""
from __future__ import annotations

from typing import Optional

from agentguard.approvals.manager import ApprovalManager
from agentguard.approvals.models import ApprovalStatus
from agentguard.audit.logger import AuditLogger
from agentguard.budget.tracker import BudgetTracker
from agentguard.context import ExecutionContext
from agentguard.decisions.engine import DecisionEngine
from agentguard.decisions.models import DecisionType, PolicyDecision
from agentguard.exceptions import ApprovalRequiredException, ToolDeniedException
from agentguard.loops.detector import LoopDetector
from agentguard.policy.engine import PolicyEngine
from agentguard.storage.database import DatabaseManager, utcnow_iso


class Interceptor:
    """
    Owns the full per-call decision sequence:

        1. Build ExecutionContext
        2. Ensure a `runs` row exists for ctx.run_id (FK requirement for
           approvals/audit_log writes)
        3. Run DecisionEngine.evaluate() -> ALLOW / DENY / REQUIRE_APPROVAL
        4. Branch:
           - DENY -> log audit record, raise ToolDeniedException
           - ALLOW -> log audit record, return (caller proceeds to call
             the real tool function)
           - REQUIRE_APPROVAL -> idempotent approval handling (see
             handle_require_approval() docstring), log audit record,
             then either return (if already APPROVED) or raise
             ApprovalRequiredException / ToolDeniedException

    This class does NOT call the wrapped tool function itself -- that is
    the job of the decorator/wrapper in AgentGuard (core.py), which calls
    Interceptor.check() first and only proceeds to call the real function
    if check() returns normally (does not raise).
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        decision_engine: DecisionEngine,
        approval_manager: ApprovalManager,
        audit_logger: AuditLogger,
        db: DatabaseManager,
        strict_run_id: bool = False,
    ):
        self._policy_engine = policy_engine
        self._decision_engine = decision_engine
        self._approval_manager = approval_manager
        self._audit_logger = audit_logger
        self._db = db
        self._strict_run_id = strict_run_id

    def check(
        self,
        tool_name: str,
        arguments: dict,
        run_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        framework: str = "unknown",
        metadata: Optional[dict] = None,
    ) -> ExecutionContext:
        """
        Run the full decision sequence for one tool call.

        Returns the ExecutionContext on success (decision was ALLOW, or
        REQUIRE_APPROVAL that has already been APPROVED) -- the caller
        should proceed to invoke the real tool function.

        Raises ToolDeniedException if the decision is DENY, or if a prior
        REQUIRE_APPROVAL request was REJECTED/EXPIRED.

        Raises ApprovalRequiredException if approval is needed and not
        yet granted (whether this is a brand new request or a still-
        PENDING one from a prior attempt).
        """
        ctx = ExecutionContext.build(
            tool_name=tool_name,
            arguments=arguments,
            policy_version=self._policy_engine.policy_version,
            run_id=run_id,
            agent_id=agent_id,
            framework=framework,
            metadata=metadata,
            strict_run_id=self._strict_run_id,
        )

        self._ensure_run_exists(ctx)

        decision = self._decision_engine.evaluate(ctx)

        if decision.decision == DecisionType.DENY:
            self._audit_logger.log_decision(ctx, decision)
            raise ToolDeniedException(
                tool_name=ctx.tool_name,
                reason=decision.reason,
                rule_matched=decision.rule_matched,
            )

        if decision.decision == DecisionType.ALLOW:
            self._audit_logger.log_decision(ctx, decision)
            return ctx

        # decision.decision == REQUIRE_APPROVAL
        return self._handle_require_approval(ctx, decision)

    def _handle_require_approval(self, ctx: ExecutionContext, decision) -> ExecutionContext:
        """
        Idempotent approval handling (EDS 4.3.4).

        Lookup by composite key (run_id, tool_name, arguments_hash,
        policy_version):
          - No existing record -> create one, log audit, raise
            ApprovalRequiredException for the new approval_id.
          - Existing PENDING -> re-raise ApprovalRequiredException for
            the SAME approval_id (no duplicate created).
          - Existing APPROVED -> log audit (decision effectively becomes
            ALLOW), return ctx so the caller proceeds.
          - Existing REJECTED or EXPIRED -> log audit as DENY, raise
            ToolDeniedException.
        """
        existing = self._approval_manager.find_existing(
            run_id=ctx.run_id,
            tool_name=ctx.tool_name,
            arguments_hash=ctx.arguments_hash,
            policy_version=ctx.policy_version,
        )

        if existing is None:
            record = self._approval_manager.create_approval(
                run_id=ctx.run_id,
                tool_name=ctx.tool_name,
                arguments_hash=ctx.arguments_hash,
                policy_version=ctx.policy_version,
            )
            self._audit_logger.log_decision(ctx, decision, approval_id=record.approval_id)
            raise ApprovalRequiredException(
                approval_id=record.approval_id,
                tool_name=ctx.tool_name,
                reason=decision.reason,
            )

        if existing.status == ApprovalStatus.PENDING:
            self._audit_logger.log_decision(ctx, decision, approval_id=existing.approval_id)
            raise ApprovalRequiredException(
                approval_id=existing.approval_id,
                tool_name=ctx.tool_name,
                reason=decision.reason,
            )

        if existing.status == ApprovalStatus.APPROVED:
            # The call is actually proceeding to execute -- log the TRUE
            # outcome (ALLOW), not the original Policy Engine label
            # (REQUIRE_APPROVAL). Logging REQUIRE_APPROVAL here would be
            # misleading: a reader querying the audit log by decision
            # type would see this row under REQUIRE_APPROVAL even though
            # the tool actually ran, with no way to distinguish it from
            # a row where execution is still pending. The original
            # rule_matched and policy_version are preserved; only the
            # decision label and reason are corrected to reflect reality.
            allow_decision = PolicyDecision(
                decision=DecisionType.ALLOW,
                rule_matched=decision.rule_matched,
                reason=(
                    f"Approval {existing.approval_id} was granted by "
                    f"{existing.approver or 'unknown approver'}; tool execution proceeds."
                ),
                policy_version=decision.policy_version,
            )
            self._audit_logger.log_decision(
                ctx,
                allow_decision,
                approval_id=existing.approval_id,
            )
            return ctx

        # REJECTED or EXPIRED -> permanently denied, no further approval
        # requests are created for this exact composite key. Log the
        # ACTUAL outcome (DENY), not the stale REQUIRE_APPROVAL label --
        # same reasoning as the APPROVED branch above.
        deny_reason = (
            f"Approval {existing.approval_id} was {existing.status.value} "
            f"and will not be re-requested for this exact call."
        )
        deny_decision = PolicyDecision(
            decision=DecisionType.DENY,
            rule_matched=decision.rule_matched,
            reason=deny_reason,
            policy_version=decision.policy_version,
        )
        self._audit_logger.log_decision(ctx, deny_decision, approval_id=existing.approval_id)
        raise ToolDeniedException(
            tool_name=ctx.tool_name,
            reason=deny_reason,
            rule_matched=decision.rule_matched,
        )

    def _ensure_run_exists(self, ctx: ExecutionContext) -> None:
        """
        Upsert a `runs` row for ctx.run_id if one doesn't already exist.

        Required because approvals.run_id and the FK relationships in the
        schema reference runs(run_id) -- without this, the first approval
        or audit write for a brand new run_id would fail with a foreign
        key constraint violation. Uses INSERT OR IGNORE so this is safe
        to call on every single tool call without raising on duplicates.
        """
        self._db.execute(
            """INSERT OR IGNORE INTO runs (run_id, created_at, framework, agent_id)
               VALUES (?, ?, ?, ?)""",
            (ctx.run_id, utcnow_iso(), ctx.framework, ctx.agent_id),
        )
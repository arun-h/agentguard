"""
Decision Engine: combines Policy Engine, Budget Tracker, and Loop Detector
output into a single final PolicyDecision.

Reference: EDS Section 4.5.4 (Enforcement Strategy) for the exact ordering:

    1. Policy Engine evaluates rules -> ALLOW, DENY, or REQUIRE_APPROVAL
    2. If result is DENY -> return DENY immediately, no budget/loop check
    3. If result is ALLOW or REQUIRE_APPROVAL -> check budget, then loop
    4. If either is exceeded/detected -> override to DENY, note why
    5. Otherwise -> return the original Policy Engine decision unchanged

This ordering means budget/loop enforcement OVERRIDES REQUIRE_APPROVAL.
A tool that would normally require human approval is denied outright if
the run's budget is exhausted or a loop is detected -- this prevents an
agent from escalating to approval as a way of bypassing budget/loop
limits (EDS 4.5.4 note).

CONCURRENCY FIX (post-review):
evaluate() previously read self._policy_engine.policy TWICE per call --
once implicitly inside policy_engine.evaluate(ctx), and once explicitly
afterward to read .budget / .loop_detection. Each read independently
acquired PolicyEngine's internal lock and was individually atomic, but
the TWO reads together were not atomic as a pair. A reload() landing
between them could mean the matched rule came from policy version N
while the budget/loop thresholds applied came from version N+1 --
silently breaking the "decision is fully explained by one policy_version"
guarantee.

Fix: capture exactly one PolicyConfig snapshot at the top of evaluate(),
via a single property read, and use that same snapshot object for rule
matching, budget config, and loop config for the remainder of the call.
"""
from __future__ import annotations

from agentguard.budget.tracker import BudgetTracker
from agentguard.context import ExecutionContext
from agentguard.decisions.models import DecisionType, PolicyDecision
from agentguard.loops.detector import LoopDetector
from agentguard.policy.engine import PolicyEngine


class DecisionEngine:
    """
    Orchestrates PolicyEngine + BudgetTracker + LoopDetector to produce
    the final decision for a tool call.
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        budget_tracker: BudgetTracker,
        loop_detector: LoopDetector,
    ):
        self._policy_engine = policy_engine
        self._budget_tracker = budget_tracker
        self._loop_detector = loop_detector

    def evaluate(self, ctx: ExecutionContext) -> PolicyDecision:
        """
        Produce the final decision for ctx.

        Side effects (budget increment, loop history recording) only
        happen when the call actually proceeds toward execution -- i.e.
        when the Policy Engine did NOT already say DENY.

        Reference: EDS 4.5.4.

        Concurrency contract: takes exactly one snapshot of the active
        policy at the start of this call (`policy_snapshot`). Every
        subsequent reference to rules, budget limits, or loop config
        within this single evaluate() invocation uses that same snapshot
        object -- never a fresh read of self._policy_engine.policy. This
        guarantees the entire decision is attributable to one consistent
        policy_version, even if PolicyEngine.reload() happens on another
        thread concurrently with this call.
        """
        # Single atomic snapshot -- the only read of self._policy_engine
        # in this entire method. Everything below uses this object.
        policy_snapshot = self._policy_engine.policy

        policy_decision = self._policy_engine.evaluate_against(ctx, policy_snapshot)

        if policy_decision.decision == DecisionType.DENY:
            # Step 2: policy already said no. Nothing else to check.
            return policy_decision

        # Step 3: ALLOW or REQUIRE_APPROVAL -- check budget and loop,
        # using limits from the SAME snapshot used for rule matching above.
        limits = policy_snapshot.budget
        budget_result = self._budget_tracker.check(ctx.run_id, limits)
        if budget_result.is_exceeded:
            return PolicyDecision(
                decision=DecisionType.DENY,
                rule_matched=policy_decision.rule_matched,
                reason=(
                    f"Budget exceeded for run {ctx.run_id!r}: "
                    f"{', '.join(budget_result.exceeded)}. "
                    f"Original policy decision was {policy_decision.decision.value}."
                ),
                policy_version=policy_decision.policy_version,
            )

        loop_config = policy_snapshot.loop_detection
        if loop_config.enabled:
            loop_result = self._loop_detector.record_and_check(ctx.run_id, ctx.tool_name)
            if loop_result.detected:
                return PolicyDecision(
                    decision=DecisionType.DENY,
                    rule_matched=policy_decision.rule_matched,
                    reason=(
                        f"Loop detected for run {ctx.run_id!r}: "
                        f"pattern={loop_result.pattern!r} count={loop_result.count}. "
                        f"Original policy decision was {policy_decision.decision.value}."
                    ),
                    policy_version=policy_decision.policy_version,
                )

        # Step 5: nothing overrode the original decision.
        # Budget increment happens on actual ALLOW (i.e. not REQUIRE_APPROVAL,
        # which has not yet been granted and may never execute).
        if policy_decision.decision == DecisionType.ALLOW:
            self._budget_tracker.increment(ctx.run_id)

        return policy_decision
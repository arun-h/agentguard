"""
Policy Engine: loads policy YAML, validates it, and evaluates rules
against incoming tool calls.

Reference: EDS Section 4.2 (Feature 2: Policy Engine).
"""
from __future__ import annotations

import threading

import yaml

from agentguard.context import ExecutionContext
from agentguard.decisions.models import DecisionType, PolicyDecision
from agentguard.exceptions import PolicyValidationError
from agentguard.policy.models import PolicyConfig


class PolicyEngine:
    """
    Owns the active policy and evaluates ExecutionContexts against it.

    Reference: EDS 4.2.4 (Loading Strategy), 4.2.5 (Evaluation Algorithm),
    4.2.6 (Hot-Reload Behavior).
    """

    def __init__(self, policy_path: str):
        self._path = policy_path
        self._lock = threading.RLock()
        self._policy: PolicyConfig = self._load_and_validate(policy_path)

    @property
    def policy_version(self) -> str:
        with self._lock:
            return self._policy.version

    @property
    def policy(self) -> PolicyConfig:
        """Return the currently active policy snapshot."""
        with self._lock:
            return self._policy

    def _load_and_validate(self, path: str) -> PolicyConfig:
        try:
            with open(path, "r") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise PolicyValidationError(
                path, "<file>", "valid YAML", f"YAML parse error: {exc}"
            ) from exc
        return PolicyConfig.from_dict(raw, source_path=path)

    def reload(self) -> None:
        """
        Thread-safe hot reload. Atomic swap on success.

        If the new policy fails validation, PolicyValidationError is raised
        and the previously active policy remains in effect — reload never
        leaves the engine in a half-updated state.

        Reference: EDS 4.2.6.
        """
        new_policy = self._load_and_validate(self._path)
        with self._lock:
            self._policy = new_policy

    def evaluate(self, ctx: ExecutionContext) -> PolicyDecision:
        """
        Evaluate a single ExecutionContext against the active policy.

        Linear scan, first match wins, exact tool name equality only
        (case-sensitive). No wildcard/regex matching in MVP.

        This reads self._policy internally via the `policy` property,
        which is correct and atomic for a STANDALONE call to evaluate().
        Callers (such as DecisionEngine) that also need to read budget
        or loop_detection config for the SAME decision should prefer
        evaluate_against() instead, passing a single snapshot obtained
        once via the `policy` property -- see evaluate_against() docstring
        for why this matters under concurrent reload().

        Reference: EDS 4.2.5.
        """
        policy = self.policy
        return self.evaluate_against(ctx, policy)

    def evaluate_against(self, ctx: ExecutionContext, policy: PolicyConfig) -> PolicyDecision:
        """
        Evaluate ctx against an EXTERNALLY supplied PolicyConfig snapshot,
        without taking any new read of self._policy.

        Why this exists: a caller that needs both the rule-matching result
        AND the budget/loop_detection config for the same decision (i.e.
        DecisionEngine) must use ONE snapshot for both, or a reload()
        landing between two separate reads could mix rule matching from
        policy version N with budget/loop thresholds from version N+1 --
        breaking the guarantee that a decision is fully explained by a
        single policy_version. Callers in that situation should do:

            policy_snapshot = policy_engine.policy   # one read
            decision = policy_engine.evaluate_against(ctx, policy_snapshot)
            limits = policy_snapshot.budget           # same snapshot
            loop_cfg = policy_snapshot.loop_detection  # same snapshot

        rather than calling evaluate() and then separately reading
        policy_engine.policy again afterward.

        Reference: EDS 4.2.5.
        """
        for rule in policy.rules:
            if rule.tool == ctx.tool_name:
                return PolicyDecision(
                    decision=DecisionType(rule.action.upper()),
                    rule_matched=rule.name,
                    reason=rule.reason or f"Rule {rule.name!r} matched",
                    policy_version=policy.version,
                )

        default_action = policy.defaults.unmatched_tool
        return PolicyDecision(
            decision=DecisionType(default_action.upper()),
            rule_matched=None,
            reason=(
                f"No rule matched tool {ctx.tool_name!r}; "
                f"default={default_action}"
            ),
            policy_version=policy.version,
        )
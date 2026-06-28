"""
Tests for agentguard.decisions.engine.DecisionEngine.

Reference:
- EDS §5.7 — Decision Engine
- EDS §5.7.1 — Decision Arbitration Order

Covers:
- Policy DENY short-circuits before budget/loop checks
- Budget exceeded overrides ALLOW and REQUIRE_APPROVAL to DENY
- Loop detected overrides ALLOW and REQUIRE_APPROVAL to DENY
- Budget increments only on actual ALLOW, not on DENY or REQUIRE_APPROVAL
- Loop history is not recorded when policy already denied
- Run isolation flows through end-to-end
"""
from __future__ import annotations

import pytest

from agentguard.budget.tracker import BudgetTracker
from agentguard.context import ExecutionContext
from agentguard.decisions.engine import DecisionEngine
from agentguard.decisions.models import DecisionType
from agentguard.loops.detector import LoopDetector
from agentguard.policy.engine import PolicyEngine


def make_ctx(tool_name: str, run_id: str = "run-1", policy_version: str = "1.0.0"):
    return ExecutionContext.build(
        tool_name=tool_name,
        arguments={},
        policy_version=policy_version,
        run_id=run_id,
    )


@pytest.fixture()
def policy_file(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text("""
version: "1.0.0"
rules:
  - name: deny_delete
    tool: delete_customer
    action: deny
  - name: approve_email
    tool: send_email
    action: require_approval
budget:
  max_tool_calls: 3
loop_detection:
  max_repetitions: 3
  window_size: 6
defaults:
  unmatched_tool: allow
""")
    return str(p)


@pytest.fixture()
def engine(policy_file):
    policy_engine = PolicyEngine(policy_file)
    budget_tracker = BudgetTracker()
    loop_detector = LoopDetector(max_repetitions=3, window_size=6)
    return DecisionEngine(policy_engine, budget_tracker, loop_detector)


class TestPolicyDenyShortCircuits:
    def test_deny_returns_immediately(self, engine):
        decision = engine.evaluate(make_ctx("delete_customer"))
        assert decision.decision == DecisionType.DENY
        assert decision.rule_matched == "deny_delete"

    def test_deny_does_not_increment_budget(self, engine):
        engine.evaluate(make_ctx("delete_customer"))
        state = engine._budget_tracker.get_state("run-1")
        assert state.calls_used == 0

    def test_deny_does_not_record_loop_history(self, engine):
        engine.evaluate(make_ctx("delete_customer"))
        history = engine._loop_detector.get_history("run-1")
        assert history == []


class TestAllowPath:
    def test_allow_increments_budget(self, engine):
        decision = engine.evaluate(make_ctx("fetch_data"))
        assert decision.decision == DecisionType.ALLOW
        state = engine._budget_tracker.get_state("run-1")
        assert state.calls_used == 1

    def test_allow_records_loop_history(self, engine):
        engine.evaluate(make_ctx("fetch_data"))
        history = engine._loop_detector.get_history("run-1")
        assert history == ["fetch_data"]

    def test_multiple_allows_accumulate_budget(self, engine):
        engine.evaluate(make_ctx("fetch_data"))
        engine.evaluate(make_ctx("other_tool"))
        state = engine._budget_tracker.get_state("run-1")
        assert state.calls_used == 2


class TestRequireApprovalPath:
    def test_require_approval_passes_through_when_under_budget(self, engine):
        decision = engine.evaluate(make_ctx("send_email"))
        assert decision.decision == DecisionType.REQUIRE_APPROVAL

    def test_require_approval_does_not_increment_budget(self, engine):
        engine.evaluate(make_ctx("send_email"))
        state = engine._budget_tracker.get_state("run-1")
        assert state.calls_used == 0

    def test_require_approval_still_records_loop_history(self, engine):
        # Loop history must be recorded even for REQUIRE_APPROVAL, since
        # the agent calling send_email repeatedly is itself a loop signal,
        # independent of whether each call ultimately gets approved.
        engine.evaluate(make_ctx("send_email"))
        history = engine._loop_detector.get_history("run-1")
        assert history == ["send_email"]


class TestBudgetOverride:
    def test_budget_exceeded_overrides_allow_to_deny(self, engine):
        # Use varied tool names so loop detection never fires here --
        # this test isolates the budget override path specifically.
        engine.evaluate(make_ctx("tool_a"))
        engine.evaluate(make_ctx("tool_b"))
        engine.evaluate(make_ctx("tool_c"))  # consumes all 3 calls
        decision = engine.evaluate(make_ctx("tool_d"))
        assert decision.decision == DecisionType.DENY
        assert "Budget exceeded" in decision.reason

    def test_budget_exceeded_overrides_require_approval_to_deny(self, engine):
        # Burn the budget using distinct allowed tools to avoid loop trigger.
        engine.evaluate(make_ctx("tool_a"))
        engine.evaluate(make_ctx("tool_b"))
        engine.evaluate(make_ctx("tool_c"))
        # Now send_email would normally REQUIRE_APPROVAL, but budget wins.
        decision = engine.evaluate(make_ctx("send_email"))
        assert decision.decision == DecisionType.DENY
        assert "Budget exceeded" in decision.reason

    def test_budget_override_preserves_original_decision_in_reason(self, engine):
        engine.evaluate(make_ctx("tool_a"))
        engine.evaluate(make_ctx("tool_b"))
        engine.evaluate(make_ctx("tool_c"))
        decision = engine.evaluate(make_ctx("send_email"))
        assert "REQUIRE_APPROVAL" in decision.reason


class TestLoopOverride:
    def test_loop_detected_overrides_allow_to_deny(self, engine):
        engine.evaluate(make_ctx("fetch_data"))
        engine.evaluate(make_ctx("fetch_data"))
        decision = engine.evaluate(make_ctx("fetch_data"))  # 3rd in a row
        assert decision.decision == DecisionType.DENY
        assert "Loop detected" in decision.reason

    def test_loop_does_not_increment_budget_on_override(self, engine):
        engine.evaluate(make_ctx("fetch_data"))
        engine.evaluate(make_ctx("fetch_data"))
        engine.evaluate(make_ctx("fetch_data"))  # triggers loop override
        state = engine._budget_tracker.get_state("run-1")
        # Only the first 2 calls (which were genuine ALLOWs) incremented budget.
        assert state.calls_used == 2


class TestRunIsolationEndToEnd:
    def test_budget_exhaustion_on_one_run_does_not_affect_another(self, engine):
        for _ in range(3):
            engine.evaluate(make_ctx("fetch_data", run_id="run-a"))
        # run-a is now exhausted; run-b should be unaffected.
        decision = engine.evaluate(make_ctx("fetch_data", run_id="run-b"))
        assert decision.decision == DecisionType.ALLOW

    def test_loop_history_on_one_run_does_not_affect_another(self, engine):
        engine.evaluate(make_ctx("fetch_data", run_id="run-a"))
        engine.evaluate(make_ctx("fetch_data", run_id="run-a"))
        engine.evaluate(make_ctx("fetch_data", run_id="run-a"))  # loop on run-a
        decision = engine.evaluate(make_ctx("fetch_data", run_id="run-b"))
        assert decision.decision == DecisionType.ALLOW


class TestLoopDetectionDisabled:
    def test_disabled_loop_detection_never_overrides(self, tmp_path):
        p = tmp_path / "policy.yaml"
        p.write_text("""
version: "1.0.0"
rules: []
loop_detection:
  enabled: false
  max_repetitions: 2
  window_size: 4
defaults:
  unmatched_tool: allow
""")
        policy_engine = PolicyEngine(str(p))
        budget_tracker = BudgetTracker()
        loop_detector = LoopDetector(max_repetitions=2, window_size=4)
        engine = DecisionEngine(policy_engine, budget_tracker, loop_detector)

        for _ in range(10):
            decision = engine.evaluate(make_ctx("same_tool"))
        assert decision.decision == DecisionType.ALLOW
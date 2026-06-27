"""
Regression test: DecisionEngine.evaluate() must use a single, consistent
PolicyConfig snapshot for the entire duration of one decision, even if
PolicyEngine.reload() happens concurrently on another thread.

Background: the original implementation called
self._policy_engine.policy.budget / .loop_detection AFTER already calling
self._policy_engine.evaluate(ctx) -- two separate locked reads of
PolicyEngine's internal state. Each read was individually atomic, but the
PAIR of reads was not atomic together. A reload() landing between them
could mean the matched rule came from policy version N while budget/loop
thresholds came from version N+1, silently breaking the guarantee that a
decision is fully explained by a single policy_version.

This test does not rely on timing luck to catch the bug -- it forces the
interleaving deterministically using a stub PolicyEngine, so the test is
not flaky and will reliably fail if the bug is reintroduced.
"""
from __future__ import annotations

import threading
import time

import pytest

from agentguard.budget.tracker import BudgetTracker
from agentguard.context import ExecutionContext
from agentguard.decisions.engine import DecisionEngine
from agentguard.decisions.models import DecisionType
from agentguard.loops.detector import LoopDetector
from agentguard.policy.engine import PolicyEngine
from agentguard.policy.models import BudgetConfig, DefaultsConfig, LoopDetectionConfig, PolicyConfig, Rule


def make_ctx(tool_name: str, run_id: str = "run-1"):
    return ExecutionContext.build(
        tool_name=tool_name, arguments={}, policy_version="irrelevant", run_id=run_id
    )


class InterleavingPolicyEngine:
    """
    Test double standing in for a real PolicyEngine. Its `.policy` property
    returns a DIFFERENT PolicyConfig object on each successive call,
    simulating a reload() happening between any two reads -- deterministically,
    not via timing.

    Version A: max_tool_calls=1 (very tight budget)
    Version B: max_tool_calls=1000 (effectively unlimited)

    If DecisionEngine reads `.policy` twice internally, the second read will
    observe Version B even though the decision should be fully attributable
    to whichever version was captured first.
    """

    def __init__(self):
        self._call_count = 0
        self._lock = threading.Lock()

        self.version_a = PolicyConfig(
            version="A",
            rules=[],
            budget=BudgetConfig(max_tool_calls=1),
            loop_detection=LoopDetectionConfig(enabled=False),
            defaults=DefaultsConfig(unmatched_tool="allow"),
        )
        self.version_b = PolicyConfig(
            version="B",
            rules=[],
            budget=BudgetConfig(max_tool_calls=1000),
            loop_detection=LoopDetectionConfig(enabled=False),
            defaults=DefaultsConfig(unmatched_tool="allow"),
        )

    @property
    def policy(self):
        with self._lock:
            self._call_count += 1
            # First read in any evaluate() call gets version A.
            # Any SECOND read (which should never happen after the fix)
            # gets version B -- proving whether a second read occurred.
            return self.version_a if self._call_count == 1 else self.version_b

    def evaluate_against(self, ctx, policy):
        from agentguard.decisions.models import PolicyDecision
        default_action = policy.defaults.unmatched_tool
        return PolicyDecision(
            decision=DecisionType(default_action.upper()),
            rule_matched=None,
            reason=f"default={default_action}",
            policy_version=policy.version,
        )

    def evaluate(self, ctx):
        # Old-style callers (not used by the fixed DecisionEngine, but
        # kept here in case anything still calls it directly).
        return self.evaluate_against(ctx, self.policy)


class TestPolicyReloadRaceClosed:
    def test_decision_engine_reads_policy_exactly_once_per_evaluate_call(self):
        """
        With the fix in place, DecisionEngine.evaluate() must read
        `policy_engine.policy` exactly ONCE per call. The stub above
        returns version A on the 1st read and version B on any
        subsequent read -- so if the bug were reintroduced (a second
        read for budget config), this test would observe budget
        limits from version B (max_tool_calls=1000) instead of
        version A (max_tool_calls=1), and the assertions below would
        catch it directly.
        """
        stub_policy_engine = InterleavingPolicyEngine()
        budget_tracker = BudgetTracker()
        loop_detector = LoopDetector(max_repetitions=5, window_size=10)
        engine = DecisionEngine(stub_policy_engine, budget_tracker, loop_detector)

        # Pre-load budget so the run is already at calls_used=1.
        budget_tracker.increment("run-1")

        decision = engine.evaluate(make_ctx("any_tool", run_id="run-1"))

        # If only ONE read happened (the fix), version A's
        # max_tool_calls=1 applies, and calls_used=1 >= 1 -> budget
        # exceeded -> DENY.
        #
        # If a SECOND read happened (the bug), version B's
        # max_tool_calls=1000 would apply instead, and the call would
        # incorrectly be ALLOWed.
        assert decision.decision == DecisionType.DENY
        assert "Budget exceeded" in decision.reason

        # Exactly one read of `.policy` must have occurred for this
        # single evaluate() call.
        assert stub_policy_engine._call_count == 1

    def test_policy_version_in_decision_matches_version_used_for_budget(self):
        """
        The policy_version on the returned decision must match the
        version whose budget config was actually applied -- i.e. no
        cross-version mixing is observable from the outside either.
        """
        stub_policy_engine = InterleavingPolicyEngine()
        budget_tracker = BudgetTracker()
        loop_detector = LoopDetector(max_repetitions=5, window_size=10)
        engine = DecisionEngine(stub_policy_engine, budget_tracker, loop_detector)

        budget_tracker.increment("run-1")
        decision = engine.evaluate(make_ctx("any_tool", run_id="run-1"))

        # The decision's reported policy_version must be "A" (the
        # snapshot actually used for both rule matching and budget),
        # never "B" (which would indicate the second, stale read).
        assert decision.policy_version == "A"


class TestPolicyReloadRaceWithRealPolicyEngine:
    """
    End-to-end version against the real PolicyEngine + real reload(),
    using many concurrent reload() calls racing many concurrent
    evaluate() calls, to additionally confirm no exception or crash
    occurs under real lock contention.
    """

    @pytest.fixture()
    def policy_file(self, tmp_path):
        p = tmp_path / "policy.yaml"
        p.write_text("""
version: "1.0.0"
rules: []
budget:
  max_tool_calls: 5
loop_detection:
  enabled: false
defaults:
  unmatched_tool: allow
""")
        return str(p)

    def test_concurrent_reload_and_evaluate_no_crash_and_consistent_versions(self, policy_file):
        policy_engine = PolicyEngine(policy_file)
        budget_tracker = BudgetTracker()
        loop_detector = LoopDetector(max_repetitions=5, window_size=10)
        engine = DecisionEngine(policy_engine, budget_tracker, loop_detector)

        stop = threading.Event()
        errors = []
        observed_versions = []
        versions_lock = threading.Lock()

        def evaluator(run_id):
            while not stop.is_set():
                try:
                    decision = engine.evaluate(make_ctx("some_tool", run_id=run_id))
                    with versions_lock:
                        observed_versions.append(decision.policy_version)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def reloader():
            toggle = False
            while not stop.is_set():
                version = "2.0.0" if toggle else "1.0.0"
                with open(policy_file, "w") as f:
                    f.write(f"""
version: "{version}"
rules: []
budget:
  max_tool_calls: 5
loop_detection:
  enabled: false
defaults:
  unmatched_tool: allow
""")
                try:
                    policy_engine.reload()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                toggle = not toggle
                time.sleep(0.001)

        threads = [
            threading.Thread(target=evaluator, args=(f"run-{i}",)) for i in range(4)
        ] + [threading.Thread(target=reloader)]

        for t in threads:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join()

        assert errors == []
        assert observed_versions, "no decisions were observed -- test setup issue"
        assert set(observed_versions) <= {"1.0.0", "2.0.0"}
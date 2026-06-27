"""
Tests for agentguard.budget.tracker.BudgetTracker.

Covers:
- Run isolation (run A's budget never affects run B's)
- increment() accumulates calls and cost correctly
- check() is read-only and correctly reports exceeded limits
- Concurrent increments from multiple threads land correctly
- reset_run() clears state
- Snapshot semantics: returned state objects are not live references
"""
from __future__ import annotations

import threading

import pytest

from agentguard.budget.tracker import BudgetTracker
from agentguard.policy.models import BudgetConfig


@pytest.fixture()
def tracker():
    return BudgetTracker()


class TestIncrement:
    def test_first_increment_starts_at_one_call(self, tracker):
        state = tracker.increment("run-a")
        assert state.calls_used == 1
        assert state.cost_used == 0.0

    def test_increment_accumulates_calls(self, tracker):
        tracker.increment("run-a")
        tracker.increment("run-a")
        state = tracker.increment("run-a")
        assert state.calls_used == 3

    def test_increment_accumulates_cost(self, tracker):
        tracker.increment("run-a", cost=1.5)
        tracker.increment("run-a", cost=2.5)
        state = tracker.get_state("run-a")
        assert state.cost_used == 4.0

    def test_default_cost_is_zero(self, tracker):
        tracker.increment("run-a")
        state = tracker.get_state("run-a")
        assert state.cost_used == 0.0

    def test_returned_state_is_a_snapshot_not_a_live_reference(self, tracker):
        state = tracker.increment("run-a")
        state.calls_used = 999  # mutate the returned object
        fresh = tracker.get_state("run-a")
        assert fresh.calls_used == 1  # internal state unaffected


class TestRunIsolation:
    def test_run_a_does_not_affect_run_b(self, tracker):
        tracker.increment("run-a")
        tracker.increment("run-a")
        tracker.increment("run-b")

        state_a = tracker.get_state("run-a")
        state_b = tracker.get_state("run-b")
        assert state_a.calls_used == 2
        assert state_b.calls_used == 1

    def test_unseen_run_returns_zero_state(self, tracker):
        state = tracker.get_state("never-seen")
        assert state.calls_used == 0
        assert state.cost_used == 0.0


class TestCheck:
    def test_check_does_not_mutate_state(self, tracker):
        tracker.increment("run-a")
        limits = BudgetConfig(max_tool_calls=10)
        tracker.check("run-a", limits)
        tracker.check("run-a", limits)
        state = tracker.get_state("run-a")
        assert state.calls_used == 1  # unaffected by check() calls

    def test_under_limit_not_exceeded(self, tracker):
        tracker.increment("run-a")
        result = tracker.check("run-a", BudgetConfig(max_tool_calls=5))
        assert result.is_exceeded is False

    def test_at_limit_is_exceeded(self, tracker):
        for _ in range(3):
            tracker.increment("run-a")
        result = tracker.check("run-a", BudgetConfig(max_tool_calls=3))
        assert result.is_exceeded is True
        assert "max_tool_calls=3" in result.exceeded

    def test_cost_limit_exceeded(self, tracker):
        tracker.increment("run-a", cost=10.0)
        result = tracker.check("run-a", BudgetConfig(max_estimated_cost=5.0))
        assert result.is_exceeded is True
        assert "max_estimated_cost=5.0" in result.exceeded

    def test_both_limits_can_be_exceeded_simultaneously(self, tracker):
        for _ in range(5):
            tracker.increment("run-a", cost=2.0)
        result = tracker.check(
            "run-a", BudgetConfig(max_tool_calls=3, max_estimated_cost=5.0)
        )
        assert len(result.exceeded) == 2

    def test_no_limits_configured_never_exceeded(self, tracker):
        for _ in range(1000):
            tracker.increment("run-a", cost=1000.0)
        result = tracker.check("run-a", BudgetConfig())
        assert result.is_exceeded is False

    def test_unseen_run_against_limits_not_exceeded(self, tracker):
        result = tracker.check("never-seen", BudgetConfig(max_tool_calls=1))
        assert result.is_exceeded is False


class TestResetRun:
    def test_reset_clears_state(self, tracker):
        tracker.increment("run-a")
        tracker.increment("run-a")
        tracker.reset_run("run-a")
        state = tracker.get_state("run-a")
        assert state.calls_used == 0

    def test_reset_unseen_run_does_not_raise(self, tracker):
        tracker.reset_run("never-existed")  # must not raise

    def test_reset_only_affects_target_run(self, tracker):
        tracker.increment("run-a")
        tracker.increment("run-b")
        tracker.reset_run("run-a")
        assert tracker.get_state("run-a").calls_used == 0
        assert tracker.get_state("run-b").calls_used == 1


class TestConcurrency:
    def test_concurrent_increments_on_same_run_all_count(self, tracker):
        """
        100 threads each call increment() once on the same run_id.
        Without correct locking, lost updates would produce a count < 100.
        """
        def worker():
            tracker.increment("shared-run")

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = tracker.get_state("shared-run")
        assert state.calls_used == 100

    def test_concurrent_increments_across_many_runs_stay_isolated(self, tracker):
        def worker(run_id, n):
            for _ in range(n):
                tracker.increment(run_id)

        threads = [
            threading.Thread(target=worker, args=(f"run-{i}", 10))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(10):
            assert tracker.get_state(f"run-{i}").calls_used == 10
"""
Tests for agentguard.loops.detector.LoopDetector.

Covers:
- Immediate repetition detection (A->A->A->A)
- Alternating cycle detection (A->B->A->B)
- Boundary cases: one call short of threshold must NOT trigger
- Run isolation
- Construction validation (max_repetitions >= 2, window_size >= max_repetitions)
- Concurrency safety
"""
from __future__ import annotations

import threading

import pytest

from agentguard.loops.detector import LoopDetector


class TestConstruction:
    def test_rejects_max_repetitions_below_2(self):
        with pytest.raises(ValueError):
            LoopDetector(max_repetitions=1, window_size=10)

    def test_rejects_window_size_below_max_repetitions(self):
        with pytest.raises(ValueError):
            LoopDetector(max_repetitions=5, window_size=3)

    def test_accepts_window_size_equal_to_max_repetitions(self):
        LoopDetector(max_repetitions=3, window_size=3)  # must not raise


class TestImmediateRepetition:
    def test_exactly_at_threshold_triggers(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        det.record_and_check("r1", "search")
        det.record_and_check("r1", "search")
        result = det.record_and_check("r1", "search")
        assert result.detected is True
        assert result.pattern == "search"
        assert result.count == 3

    def test_one_short_of_threshold_does_not_trigger(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        det.record_and_check("r1", "search")
        result = det.record_and_check("r1", "search")
        assert result.detected is False

    def test_interrupted_sequence_resets_repetition_count(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        det.record_and_check("r1", "search")
        det.record_and_check("r1", "search")
        det.record_and_check("r1", "fetch")  # breaks the streak
        result = det.record_and_check("r1", "search")
        assert result.detected is False  # only 1 consecutive "search" since break

    def test_exceeding_threshold_still_triggers(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        for _ in range(5):
            result = det.record_and_check("r1", "search")
        assert result.detected is True


class TestAlternatingCycle:
    def test_alternating_pattern_triggers(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        sequence = ["a", "b", "a", "b", "a", "b"]
        results = [det.record_and_check("r1", t) for t in sequence]
        assert results[-1].detected is True
        assert results[-1].pattern == "a-b"

    def test_alternating_pattern_one_short_does_not_trigger(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        sequence = ["a", "b", "a", "b", "a"]  # 5 calls, need 6 for max_reps=3
        results = [det.record_and_check("r1", t) for t in sequence]
        assert all(r.detected is False for r in results)

    def test_three_distinct_tools_does_not_trigger_alternation(self):
        det = LoopDetector(max_repetitions=2, window_size=10)
        sequence = ["a", "b", "c", "a", "b", "c"]
        results = [det.record_and_check("r1", t) for t in sequence]
        # Neither immediate repetition nor a 2-cycle pattern exists here.
        assert all(r.detected is False for r in results)


class TestRunIsolation:
    def test_run_a_history_does_not_affect_run_b(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        det.record_and_check("run-a", "search")
        det.record_and_check("run-a", "search")
        det.record_and_check("run-a", "search")  # triggers for run-a

        result_b = det.record_and_check("run-b", "search")
        assert result_b.detected is False  # run-b has only seen 1 call

    def test_get_history_returns_per_run_snapshot(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        det.record_and_check("run-a", "x")
        det.record_and_check("run-a", "y")
        det.record_and_check("run-b", "z")
        assert det.get_history("run-a") == ["x", "y"]
        assert det.get_history("run-b") == ["z"]

    def test_unseen_run_history_is_empty(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        assert det.get_history("never-seen") == []


class TestWindowSizeBound:
    def test_window_does_not_grow_unbounded(self):
        det = LoopDetector(max_repetitions=3, window_size=5)
        for i in range(100):
            det.record_and_check("r1", f"tool-{i}")
        assert len(det.get_history("r1")) == 5  # capped at window_size


class TestResetRun:
    def test_reset_clears_history(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        det.record_and_check("r1", "search")
        det.record_and_check("r1", "search")
        det.reset_run("r1")
        result = det.record_and_check("r1", "search")
        assert result.detected is False  # history was cleared, only 1 call now

    def test_reset_unseen_run_does_not_raise(self):
        det = LoopDetector(max_repetitions=3, window_size=10)
        det.reset_run("never-existed")  # must not raise


class TestConcurrency:
    def test_concurrent_calls_on_same_run_do_not_corrupt_history(self):
        det = LoopDetector(max_repetitions=3, window_size=50)
        errors = []

        def worker():
            try:
                det.record_and_check("shared-run", "tool")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(det.get_history("shared-run")) == 50
"""
Budget Tracker: per-run accumulation and limit enforcement.

Reference:
- EDS §5.5 — Budget Controls
- EDS §7.1 — Run Isolation, Concurrency, and Thread Safety: Isolation by Subsystem
- EDS §7.5 — Run Isolation, Concurrency, and Thread Safety: Durability — Explicit Asymmetry
"""
from __future__ import annotations

import dataclasses
import threading
from typing import Dict

from agentguard.budget.models import BudgetCheckResult, BudgetState
from agentguard.policy.models import BudgetConfig

# IMPORTANT (EDS 4.5.7 / 9.3): Budget state lives only in process memory.
# It is NOT persisted to SQLite. A process restart loses all accumulated
# budget state for every run. This is a deliberate, documented MVP
# limitation -- do not rely on budget controls as a hard safety boundary
# in environments where process restarts can happen mid-run.


class BudgetTracker:
    """
    Tracks calls_used and cost_used per run_id, isolated so that one
    run's accumulation never affects another run's accumulation.
    """

    def __init__(self):
        self._state: Dict[str, BudgetState] = {}
        self._lock = threading.RLock()

    def increment(self, run_id: str, cost: float = 0.0) -> BudgetState:
        """
        Record one tool call (and optional cost) against run_id.

        Returns a snapshot (copy) of the resulting state, not a live
        reference, so callers cannot mutate internal tracker state.
        """
        with self._lock:
            if run_id not in self._state:
                self._state[run_id] = BudgetState()
            state = self._state[run_id]
            state.calls_used += 1
            state.cost_used += cost
            return dataclasses.replace(state)

    def check(self, run_id: str, limits: BudgetConfig) -> BudgetCheckResult:
        """
        Check the current accumulated state for run_id against limits.

        Does NOT increment. Read-only. Callers decide whether to call
        increment() based on the overall decision (see Decision Engine).
        """
        with self._lock:
            state = dataclasses.replace(self._state.get(run_id, BudgetState()))

        exceeded = []
        if limits.max_tool_calls is not None and state.calls_used >= limits.max_tool_calls:
            exceeded.append(f"max_tool_calls={limits.max_tool_calls}")
        if (
            limits.max_estimated_cost is not None
            and state.cost_used >= limits.max_estimated_cost
        ):
            exceeded.append(f"max_estimated_cost={limits.max_estimated_cost}")

        return BudgetCheckResult(exceeded=exceeded, state=state)

    def get_state(self, run_id: str) -> BudgetState:
        """Return a snapshot of current state for run_id (zeros if unseen)."""
        with self._lock:
            return dataclasses.replace(self._state.get(run_id, BudgetState()))

    def reset_run(self, run_id: str) -> None:
        """
        Clear in-memory budget state for run_id.

        Call this when a run completes to free memory in long-running
        applications. There is no automatic expiration (EDS 4.5.6).
        """
        with self._lock:
            self._state.pop(run_id, None)
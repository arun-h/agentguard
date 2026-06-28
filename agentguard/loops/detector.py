"""
Loop Detector: per-run pattern detection for repeated tool calls.

Reference:
- EDS §5.6 — Loop Detection
- EDS §7.5 — Run Isolation, Concurrency, and Thread Safety: Durability — Explicit Asymmetry

IMPORTANT: Loop detection history lives only in process memory. It is NOT
persisted to SQLite. A process restart loses all call history for every
run. Do not rely on loop detection as a hard safety boundary in
environments where process restarts can happen mid-run.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict

from agentguard.loops.models import LoopCheckResult


class LoopDetector:
    """
    Detects two patterns per run_id:
      1. Immediate repetition: A -> A -> A -> A (>= max_repetitions times)
      2. Alternating cycle of length 2: A -> B -> A -> B (>= max_repetitions
         repetitions of the pair)
    """

    def __init__(self, max_repetitions: int = 5, window_size: int = 20):
        if max_repetitions < 2:
            raise ValueError("max_repetitions must be >= 2")
        if window_size < max_repetitions:
            raise ValueError("window_size must be >= max_repetitions")
        self._max_reps = max_repetitions
        self._window = window_size
        self._windows: Dict[str, Deque[str]] = {}
        self._lock = threading.RLock()

    def record_and_check(self, run_id: str, tool_name: str) -> LoopCheckResult:
        """
        Append tool_name to run_id's history window and check for a loop.

        This both records AND checks in one call (matching EDS naming);
        there is no separate "check without recording" mode, since loop
        detection inherently needs to see every call in sequence.
        """
        with self._lock:
            if run_id not in self._windows:
                self._windows[run_id] = deque(maxlen=self._window)
            win = self._windows[run_id]
            win.append(tool_name)
            history = list(win)

        # Immediate repetition check.
        tail = history[-self._max_reps:]
        if len(tail) == self._max_reps and all(t == tool_name for t in tail):
            return LoopCheckResult(detected=True, pattern=tool_name, count=self._max_reps)

        # Alternating cycle check (fixed length 2 in MVP).
        n = self._max_reps * 2
        if len(history) >= n:
            tail2 = history[-n:]
            evens = tail2[0::2]
            odds = tail2[1::2]
            if len(set(evens)) == 1 and len(set(odds)) == 1 and evens[0] != odds[0]:
                pattern = f"{evens[0]}-{odds[0]}"
                return LoopCheckResult(detected=True, pattern=pattern, count=self._max_reps)

        return LoopCheckResult(detected=False, pattern=None, count=0)

    def reset_run(self, run_id: str) -> None:
        """Clear in-memory call history for run_id."""
        with self._lock:
            self._windows.pop(run_id, None)

    def get_history(self, run_id: str) -> list[str]:
        """Return a snapshot of the current call history window for run_id."""
        with self._lock:
            win = self._windows.get(run_id)
            return list(win) if win is not None else []
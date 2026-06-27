"""
Budget tracking models.

Reference: EDS Section 4.5 (Feature 5: Budget Controls).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class BudgetState:
    calls_used: int = 0
    cost_used: float = 0.0


@dataclass
class BudgetCheckResult:
    exceeded: List[str] = field(default_factory=list)
    state: BudgetState = field(default_factory=BudgetState)

    @property
    def is_exceeded(self) -> bool:
        return len(self.exceeded) > 0
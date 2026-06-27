"""
Loop detection result model.

Reference: EDS Section 4.6 (Feature 6: Loop Detection).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class LoopCheckResult:
    detected: bool
    pattern: Optional[str] = None
    count: int = 0
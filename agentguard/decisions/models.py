"""
Decision output types.

Reference: EDS Section 3.4 (PolicyDecision Object).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class DecisionType(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


@dataclass
class PolicyDecision:
    decision: DecisionType
    reason: str
    policy_version: str
    rule_matched: Optional[str] = None
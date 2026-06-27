"""
Approval workflow models.

Reference: EDS Section 4.3.2 (Approval Lifecycle - State Machine).

State machine (no backward transitions, all terminal states are final):

    PENDING -> APPROVED   (human grants approval)
    PENDING -> REJECTED   (human rejects approval)
    PENDING -> EXPIRED    (TTL elapsed with no decision)
    APPROVED / REJECTED / EXPIRED -> (terminal, no further transitions)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self != ApprovalStatus.PENDING


@dataclass
class ApprovalRecord:
    """
    Mirrors one row of the `approvals` SQLite table (EDS Section 5.3).
    """

    approval_id: str
    run_id: str
    tool_name: str
    arguments_hash: str
    policy_version: str
    status: ApprovalStatus
    created_at: datetime
    ttl_seconds: int = 3600
    updated_at: Optional[datetime] = None
    approver: Optional[str] = None
    notes: Optional[str] = None

    def is_expired(self, now: datetime) -> bool:
        """
        Lazy expiration check (EDS 4.3.5): only meaningful for PENDING
        records. Terminal states (APPROVED/REJECTED/EXPIRED) cannot
        "become" expired after the fact -- their status is already final.
        """
        if self.status != ApprovalStatus.PENDING:
            return False
        age_seconds = (now - self.created_at).total_seconds()
        return age_seconds > self.ttl_seconds
"""
Audit record model.

Reference:
- EDS §3.5 — Core Data Models: AuditRecord
- EDS §6.2 — SQLite Storage: Database Schema
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class AuditRecord:
    """
    Mirrors one row of the `audit_log` SQLite table.

    `id` is None until the record has actually been persisted (it's an
    autoincrement primary key assigned by SQLite on insert) -- a freshly
    constructed AuditRecord that hasn't been written yet has id=None.
    """

    run_id: str
    timestamp: datetime
    tool_name: str
    arguments_hash: str
    policy_version: str
    decision: str
    reason: str
    id: Optional[int] = None
    approval_id: Optional[str] = None
    rule_matched: Optional[str] = None
    budget_calls_used: Optional[int] = None
    budget_cost_used: Optional[float] = None
    loop_count: Optional[int] = None
    agent_id: Optional[str] = None
    framework: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
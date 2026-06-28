"""
AgentGuard: runtime governance middleware for AI agents.

Public API surface.

Reference:
- EDS §8.1 — Public API: Construction
"""
from agentguard.core import AgentGuard
from agentguard.exceptions import (
    AgentGuardDatabaseError,
    AgentGuardException,
    ApprovalRequiredException,
    AuditMutationError,
    BudgetExceededException,
    LoopDetectedException,
    MissingRunIdError,
    PolicyValidationError,
    ToolDeniedException,
)

__all__ = [
    "AgentGuard",
    "AgentGuardException",
    "ToolDeniedException",
    "ApprovalRequiredException",
    "BudgetExceededException",
    "LoopDetectedException",
    "PolicyValidationError",
    "AgentGuardDatabaseError",
    "AuditMutationError",
    "MissingRunIdError",
]
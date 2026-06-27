"""
AgentGuard: runtime governance middleware for AI agents.

Public API surface. Reference: EDS Section 7.6.
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
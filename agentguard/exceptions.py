"""
All AgentGuard exception types.

Reference:
- EDS §5.1.5 — Tool Interception: Exception Types
- EDS §10 — Error Handling
"""
from __future__ import annotations


class AgentGuardException(Exception):
    """Base class for all AgentGuard exceptions."""
    pass


class ToolDeniedException(AgentGuardException):
    """Raised when the Decision Engine returns DENY for a tool call."""

    def __init__(self, tool_name: str, reason: str, rule_matched: str | None = None):
        self.tool_name = tool_name
        self.reason = reason
        self.rule_matched = rule_matched
        super().__init__(f"Tool {tool_name!r} denied: {reason}")


class ApprovalRequiredException(AgentGuardException):
    """
    Raised when the Decision Engine returns REQUIRE_APPROVAL.

    The host framework is expected to catch this, persist/pause agent state,
    and resume execution later once the approval has been resolved.
    """

    def __init__(self, approval_id: str, tool_name: str, reason: str):
        self.approval_id = approval_id
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(
            f"Approval required for tool {tool_name!r} (approval_id={approval_id}): {reason}"
        )


class BudgetExceededException(AgentGuardException):
    """Raised when a run exceeds a configured budget limit."""

    def __init__(self, run_id: str, limit_type: str, limit: float, current: float):
        self.run_id = run_id
        self.limit_type = limit_type
        self.limit = limit
        self.current = current
        super().__init__(
            f"Run {run_id!r} exceeded {limit_type}={limit} (current={current})"
        )


class LoopDetectedException(AgentGuardException):
    """Raised when the Loop Detector identifies a repetitive call pattern."""

    def __init__(self, run_id: str, pattern: str, count: int):
        self.run_id = run_id
        self.pattern = pattern
        self.count = count
        super().__init__(
            f"Run {run_id!r} triggered loop detection: pattern={pattern!r} count={count}"
        )


class PolicyValidationError(AgentGuardException):
    """Raised when a policy YAML file fails schema validation."""

    def __init__(self, path: str, field: str, expected: str, actual: str):
        self.path = path
        self.field = field
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Policy validation failed for {path!r}: field {field!r} "
            f"expected {expected}, got {actual!r}"
        )


class AgentGuardDatabaseError(AgentGuardException):
    """Raised on unrecoverable SQLite errors (corruption, lock timeout exhaustion)."""
    pass


class AuditMutationError(AgentGuardException):
    """Raised if code attempts to UPDATE or DELETE an audit_log row."""
    pass


class MissingRunIdError(AgentGuardException):
    """
    Raised when run_id cannot be determined and strict_run_id=True.

    Reference: EDS Section 4.1.4 (strict mode).
    """

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(
            f"run_id is required but was not found when calling tool {tool_name!r}. "
            f"Call agentguard.context.set_run_id() before invoking the agent, or pass "
            f"run_id as a keyword argument to the tool. Set strict_run_id=False on "
            f"AgentGuard.from_policy() to suppress this error and use a synthetic "
            f"run_id instead."
        )
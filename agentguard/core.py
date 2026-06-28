"""
AgentGuard: the public-facing entry point.

Reference:
- EDS §8 — Public API
- EDS §8.1 — Public API: Construction

This module constructs every subsystem exactly once per AgentGuard
instance and exposes the friendly decorator/wrapper API described in
the project README. Construction order matters: DatabaseManager first
(everything else depends on it), then PolicyEngine (reads the policy
file), then the stateless-config-dependent Budget/Loop trackers, then
DecisionEngine (composes Policy+Budget+Loop), then ApprovalManager and
AuditLogger (both depend on DatabaseManager), then Interceptor (composes
everything above it).
"""
from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, Callable, List, Optional

from agentguard.approvals.manager import ApprovalManager
from agentguard.approvals.models import ApprovalRecord
from agentguard.audit.logger import AuditLogger
from agentguard.budget.tracker import BudgetTracker
from agentguard.decisions.engine import DecisionEngine
from agentguard.interceptor import Interceptor
from agentguard.loops.detector import LoopDetector
from agentguard.policy.engine import PolicyEngine
from agentguard.storage.database import DatabaseManager


class AgentGuard:
    """
    Primary entry point. Construct via AgentGuard.from_policy(), not
    directly via __init__ (the constructor takes already-built
    subsystem instances, mainly to make testing with substitute
    components straightforward; from_policy() is the ergonomic path
    real users should call).
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        decision_engine: DecisionEngine,
        approval_manager: ApprovalManager,
        audit_logger: AuditLogger,
        db: DatabaseManager,
        interceptor: Interceptor,
    ):
        self._policy_engine = policy_engine
        self._decision_engine = decision_engine
        self._approval_manager = approval_manager
        self._audit_logger = audit_logger
        self._db = db
        self._interceptor = interceptor

    @classmethod
    def from_policy(
        cls,
        policy_path: str,
        db_path: str = "./agentguard.db",
        approval_ttl: int = 3600,
        strict_run_id: bool = False,
    ) -> "AgentGuard":
        """
        Construct a fully-wired AgentGuard instance from a policy YAML
        file and a SQLite database path.

        Reference: EDS Section 7.6.

        strict_run_id=False (default): a tool call with no resolvable
        run_id gets a synthetic one and a warning is logged.
        strict_run_id=True (recommended for production): a tool call
        with no resolvable run_id raises MissingRunIdError immediately.
        """
        db = DatabaseManager(db_path)
        policy_engine = PolicyEngine(policy_path)
        budget_tracker = BudgetTracker()
        loop_detector = LoopDetector(
            max_repetitions=policy_engine.policy.loop_detection.max_repetitions,
            window_size=policy_engine.policy.loop_detection.window_size,
        )
        decision_engine = DecisionEngine(policy_engine, budget_tracker, loop_detector)
        approval_manager = ApprovalManager(db, default_ttl_seconds=approval_ttl)
        audit_logger = AuditLogger(db)
        interceptor = Interceptor(
            policy_engine,
            decision_engine,
            approval_manager,
            audit_logger,
            db,
            strict_run_id=strict_run_id,
        )
        return cls(
            policy_engine, decision_engine, approval_manager, audit_logger, db, interceptor
        )

    # ------------------------------------------------------------------
    # Tool wrapping (the core public API)
    # ------------------------------------------------------------------

    def tool(self, fn: Optional[Callable] = None, *, run_id_param: str = "run_id") -> Callable:
        """
        Decorator. Works on both sync and async functions -- detected
        automatically via inspect.iscoroutinefunction(), so callers never
        need to pick a different decorator for async tools.

        Usage:
            @guard.tool
            def my_tool(x: int) -> int: ...

            @guard.tool
            async def my_async_tool(x: int) -> int: ...

        run_id_param controls which keyword argument name, if present in
        the call, is treated as an explicit run_id override and stripped
        before calling the real function (since the real function's own
        signature almost certainly does not have a run_id parameter).
        If run_id_param is not supplied as a kwarg, run_id resolution
        falls through to the standard ContextVar/thread-local/synthetic
        chain (EDS 7.7), handled inside Interceptor/ExecutionContext.

        Supports being used both as @guard.tool and @guard.tool(...) --
        i.e. with or without parentheses.
        """
        if fn is None:
            # Called as @guard.tool(run_id_param="...") -- return a
            # decorator that will be applied to the actual function next.
            return functools.partial(self.tool, run_id_param=run_id_param)
        return self.wrap(fn, run_id_param=run_id_param)

    def wrap(self, fn: Callable, *, run_id_param: str = "run_id") -> Callable:
        """
        Programmatic wrapper. Same behavior as the @tool decorator, for
        cases where the caller cannot add a decorator to the tool's
        source (third-party tools, dynamically generated functions).

        Reference: EDS Section 7.2 (Wrapper-Based API), 7.4 (tradeoff
        analysis -- both APIs are implemented; wrap() is the underlying
        mechanism, tool()/the decorator is the recommended primary
        interface).
        """
        if asyncio.iscoroutinefunction(fn):
            return self._wrap_async(fn, run_id_param)
        return self._wrap_sync(fn, run_id_param)

    def _wrap_sync(self, fn: Callable, run_id_param: str) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            run_id = kwargs.get(run_id_param)
            call_kwargs = {k: v for k, v in kwargs.items() if k != run_id_param}
            arguments = self._bind_call_arguments(fn, args, call_kwargs)
            self._interceptor.check(
                tool_name=fn.__name__,
                arguments=arguments,
                run_id=run_id,
            )
            # check() raised if the call should not proceed; reaching
            # here means ALLOW or an already-APPROVED resume. Call the
            # real function with run_id_param stripped (the real
            # function's signature does not have this parameter).
            return fn(*args, **call_kwargs)

        return wrapper

    def _wrap_async(self, fn: Callable, run_id_param: str) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            run_id = kwargs.get(run_id_param)
            call_kwargs = {k: v for k, v in kwargs.items() if k != run_id_param}
            arguments = self._bind_call_arguments(fn, args, call_kwargs)
            self._interceptor.check(
                tool_name=fn.__name__,
                arguments=arguments,
                run_id=run_id,
            )
            return await fn(*args, **call_kwargs)

        return wrapper

    @staticmethod
    def _bind_call_arguments(fn: Callable, args: tuple, kwargs: dict) -> dict:
        """
        Produce a single dict of {param_name: value} representing this
        call, combining positional and keyword arguments via the real
        function's own signature. This dict is what gets hashed for
        approval idempotency and stored (hashed, not raw) in the audit
        log -- so positional and keyword calls with the same logical
        arguments must hash identically.

        If binding fails for any reason (e.g. *args/**kwargs-only
        signatures that don't map cleanly), falls back to treating
        positional args as a tuple under a synthetic key, combined with
        the raw kwargs -- this keeps the system available rather than
        crashing on unusual function signatures, at the cost of slightly
        less precise idempotency for those specific functions.
        """
        try:
            sig = inspect.signature(fn)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            result = dict(kwargs)
            if args:
                result["_positional_args"] = args
            return result

    # ------------------------------------------------------------------
    # Approval resolution (EDS Section 4.3.6)
    # ------------------------------------------------------------------

    def approve(self, approval_id: str, approver: str, notes: str = "") -> bool:
        """Approve a pending approval request. See ApprovalManager.approve()."""
        return self._approval_manager.approve(approval_id, approver=approver, notes=notes)

    def reject(self, approval_id: str, approver: str, notes: str = "") -> bool:
        """Reject a pending approval request. See ApprovalManager.reject()."""
        return self._approval_manager.reject(approval_id, approver=approver, notes=notes)

    def get_pending_approvals(self) -> List[ApprovalRecord]:
        """Return all PENDING approvals across all runs."""
        return self._approval_manager.get_pending()

    def get_approval(self, approval_id: str) -> Optional[ApprovalRecord]:
        """Return any approval record by ID regardless of status."""
        return self._approval_manager.get_by_id(approval_id)

    # ------------------------------------------------------------------
    # Policy management
    # ------------------------------------------------------------------

    def reload_policy(self) -> None:
        """
        Hot-reload the policy file. Atomic swap on success; a failed
        reload leaves the previously active policy in effect.

        Note: the LoopDetector's max_repetitions/window_size are fixed
        at AgentGuard construction time (from_policy()) and are NOT
        re-read on reload(). If a reloaded policy changes loop_detection
        settings, those new settings affect the BUDGET CHECK's reasoning
        about loop_detection.enabled (read fresh from the policy snapshot
        each call, per the DecisionEngine race fix), but the LoopDetector
        instance's own thresholds remain whatever they were constructed
        with. This is a known limitation, not yet addressed -- changing
        loop thresholds at runtime would require reconstructing the
        LoopDetector, which would also discard its in-memory history.
        """
        self._policy_engine.reload()

    @property
    def policy_version(self) -> str:
        return self._policy_engine.policy_version

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def reset_run(self, run_id: str) -> None:
        """
        Clear in-memory budget and loop-detection state for run_id.

        Does NOT delete any persisted approvals or audit records --
        those remain in SQLite permanently (EDS 4.4.3 immutability).
        Call this when a run completes to free memory in long-running
        applications.
        """
        self._decision_engine._budget_tracker.reset_run(run_id)
        self._decision_engine._loop_detector.reset_run(run_id)

    def get_run_audit(self, run_id: str):
        """Return all audit records for a given run, in insertion order."""
        return self._audit_logger.get_by_run_id(run_id)
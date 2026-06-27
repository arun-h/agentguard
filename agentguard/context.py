"""
Execution context construction and run_id propagation.

Reference: EDS Section 3.3 (ExecutionContext), Section 4.1.4 (Missing run_id
Handling / strict_run_id), Section 7.7 (run_id Propagation).

Resolution order for run_id, per EDS 7.7:
    explicit keyword argument > ContextVar > thread-local > synthetic fallback
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from agentguard.exceptions import MissingRunIdError

logger = logging.getLogger("agentguard")

# ----------------------------------------------------------------------
# run_id propagation primitives
# ----------------------------------------------------------------------

_run_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agentguard_run_id", default=None
)
_run_id_thread_local = threading.local()


def set_run_id(run_id: str) -> None:
    """
    Set the current run_id for this async context AND this thread.

    Call this once before invoking an agent run. In asyncio code, the
    ContextVar value propagates correctly into child tasks created with
    asyncio.create_task() that inherit the current context. In plain
    threaded code, the thread-local fallback covers frameworks that spawn
    raw threads without copying context.
    """
    _run_id_var.set(run_id)
    _run_id_thread_local.run_id = run_id


def get_current_run_id() -> Optional[str]:
    """Read run_id via ContextVar first, then thread-local. None if unset."""
    value = _run_id_var.get()
    if value is not None:
        return value
    return getattr(_run_id_thread_local, "run_id", None)


def clear_run_id() -> None:
    """Clear run_id from both propagation mechanisms. Useful in tests."""
    _run_id_var.set(None)
    _run_id_thread_local.run_id = None


def resolve_run_id(
    explicit: Optional[str],
    tool_name: str,
    strict_run_id: bool = False,
) -> tuple[str, bool]:
    """
    Resolve the run_id to use for this tool call.

    Resolution order (EDS 7.7): explicit kwarg > ContextVar > thread-local
    > synthetic fallback.

    Returns (run_id, was_synthetic).

    Raises MissingRunIdError if strict_run_id=True and no run_id could be
    resolved from any source.
    """
    if explicit:
        return explicit, False

    found = get_current_run_id()
    if found:
        return found, False

    if strict_run_id:
        raise MissingRunIdError(tool_name)

    synthetic = f"auto-{uuid.uuid4()}"
    logger.warning(
        "run_id not found for tool %r; using synthetic run_id %r. "
        "Cross-run isolation guarantees do not apply to synthetic run_ids. "
        "Set strict_run_id=True to treat this as an error instead.",
        tool_name,
        synthetic,
    )
    return synthetic, True


# ----------------------------------------------------------------------
# Canonical argument hashing
# ----------------------------------------------------------------------

def compute_arguments_hash(arguments: Dict[str, Any]) -> str:
    """
    SHA-256 of canonical JSON of arguments (sorted keys, no extra whitespace).

    Reference: EDS Section 3.3. Used by approval idempotency lookups,
    audit deduplication, and loop detection.

    Non-JSON-serializable values are converted via str() as a fallback so
    this never raises on unusual argument types; this is a deliberate
    trade-off favoring availability over hash precision for edge-case types.
    """
    canonical = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# ExecutionContext
# ----------------------------------------------------------------------

@dataclass
class ExecutionContext:
    """
    Represents a single intercepted tool call.

    Reference: EDS Section 3.3.
    """

    run_id: str
    tool_name: str
    arguments: Dict[str, Any]
    arguments_hash: str
    timestamp: datetime
    policy_version: str
    agent_id: Optional[str] = None
    framework: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)
    run_id_is_synthetic: bool = False

    @classmethod
    def build(
        cls,
        tool_name: str,
        arguments: Dict[str, Any],
        policy_version: str,
        run_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        framework: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
        strict_run_id: bool = False,
    ) -> "ExecutionContext":
        """
        Construct an ExecutionContext, resolving run_id per the standard
        resolution order and computing the arguments hash.
        """
        resolved_run_id, was_synthetic = resolve_run_id(
            explicit=run_id, tool_name=tool_name, strict_run_id=strict_run_id
        )
        return cls(
            run_id=resolved_run_id,
            tool_name=tool_name,
            arguments=arguments,
            arguments_hash=compute_arguments_hash(arguments),
            timestamp=utcnow(),
            policy_version=policy_version,
            agent_id=agent_id,
            framework=framework,
            metadata=metadata or {},
            run_id_is_synthetic=was_synthetic,
        )
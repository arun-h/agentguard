"""
Tests for agentguard.context.

Covers:
- run_id resolution order (explicit > ContextVar > thread-local > synthetic)
- strict_run_id raising MissingRunIdError
- arguments_hash determinism and sorted-key canonicalization
- ExecutionContext.build() end-to-end
"""
from __future__ import annotations

import threading

import pytest

from agentguard.context import (
    ExecutionContext,
    clear_run_id,
    compute_arguments_hash,
    resolve_run_id,
    set_run_id,
)
from agentguard.exceptions import MissingRunIdError


@pytest.fixture(autouse=True)
def _clean_run_id():
    clear_run_id()
    yield
    clear_run_id()


class TestArgumentsHash:
    def test_deterministic_for_same_input(self):
        h1 = compute_arguments_hash({"a": 1, "b": 2})
        h2 = compute_arguments_hash({"a": 1, "b": 2})
        assert h1 == h2

    def test_key_order_does_not_affect_hash(self):
        h1 = compute_arguments_hash({"a": 1, "b": 2})
        h2 = compute_arguments_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_different_values_produce_different_hash(self):
        h1 = compute_arguments_hash({"amount": 100})
        h2 = compute_arguments_hash({"amount": 200})
        assert h1 != h2

    def test_empty_arguments_hashable(self):
        h = compute_arguments_hash({})
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex digest length

    def test_non_json_native_value_does_not_raise(self):
        class Weird:
            def __str__(self):
                return "weird-object"

        h = compute_arguments_hash({"x": Weird()})
        assert isinstance(h, str)


class TestRunIdResolution:
    def test_explicit_takes_priority(self):
        set_run_id("from-context-var")
        run_id, synthetic = resolve_run_id(explicit="explicit-id", tool_name="t")
        assert run_id == "explicit-id"
        assert synthetic is False

    def test_falls_back_to_context_var(self):
        set_run_id("from-context-var")
        run_id, synthetic = resolve_run_id(explicit=None, tool_name="t")
        assert run_id == "from-context-var"
        assert synthetic is False

    def test_falls_back_to_thread_local_when_no_context_var(self):
        # Simulate a raw thread that doesn't inherit contextvars context.
        results = {}

        def worker():
            set_run_id("thread-local-id")
            run_id, synthetic = resolve_run_id(explicit=None, tool_name="t")
            results["run_id"] = run_id
            results["synthetic"] = synthetic

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert results["run_id"] == "thread-local-id"
        assert results["synthetic"] is False

    def test_generates_synthetic_when_nothing_set_and_not_strict(self):
        run_id, synthetic = resolve_run_id(explicit=None, tool_name="t")
        assert run_id.startswith("auto-")
        assert synthetic is True

    def test_raises_when_strict_and_nothing_set(self):
        with pytest.raises(MissingRunIdError) as exc_info:
            resolve_run_id(explicit=None, tool_name="wire_transfer", strict_run_id=True)
        assert "wire_transfer" in str(exc_info.value)

    def test_strict_mode_still_honors_explicit(self):
        run_id, synthetic = resolve_run_id(
            explicit="explicit-id", tool_name="t", strict_run_id=True
        )
        assert run_id == "explicit-id"
        assert synthetic is False

    def test_strict_mode_still_honors_context_var(self):
        set_run_id("ctx-id")
        run_id, synthetic = resolve_run_id(explicit=None, tool_name="t", strict_run_id=True)
        assert run_id == "ctx-id"
        assert synthetic is False


class TestExecutionContextBuild:
    def test_build_produces_complete_context(self):
        ctx = ExecutionContext.build(
            tool_name="wire_transfer",
            arguments={"account_id": "ACC1", "amount": 100.0},
            policy_version="1.0.0",
            run_id="run-1",
            framework="langgraph",
        )
        assert ctx.tool_name == "wire_transfer"
        assert ctx.run_id == "run-1"
        assert ctx.policy_version == "1.0.0"
        assert ctx.framework == "langgraph"
        assert ctx.run_id_is_synthetic is False
        assert len(ctx.arguments_hash) == 64

    def test_build_flags_synthetic_run_id(self):
        ctx = ExecutionContext.build(
            tool_name="t", arguments={}, policy_version="1.0.0"
        )
        assert ctx.run_id_is_synthetic is True
        assert ctx.run_id.startswith("auto-")

    def test_build_raises_in_strict_mode_without_run_id(self):
        with pytest.raises(MissingRunIdError):
            ExecutionContext.build(
                tool_name="t",
                arguments={},
                policy_version="1.0.0",
                strict_run_id=True,
            )

    def test_metadata_defaults_to_empty_dict(self):
        ctx = ExecutionContext.build(
            tool_name="t", arguments={}, policy_version="1.0.0", run_id="r1"
        )
        assert ctx.metadata == {}
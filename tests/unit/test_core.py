"""
Tests for agentguard.core.AgentGuard.

Covers:
- from_policy() constructs a fully working instance
- @guard.tool decorator works with and without parentheses
- Sync and async tool functions both work via the same decorator
- The real wrapped function is NEVER called when the decision is DENY
  or REQUIRE_APPROVAL (only called on ALLOW or an already-APPROVED resume)
- run_id is correctly stripped before calling the real function (no
  TypeError from an unexpected keyword argument)
- REGRESSION: positional and keyword calls with identical logical
  arguments must produce the SAME idempotency hash / approval_id --
  this was a real bug found during verification (signature binding was
  attempted with run_id still present in kwargs, causing it to fail and
  fall into an inconsistent fallback path)
- wrap() works as the non-decorator alternative
- approve()/reject()/get_pending_approvals()/get_approval() convenience
  methods delegate correctly
- reset_run() clears budget/loop state but does not touch persisted
  audit/approval records
- reload_policy() hot-reloads without disrupting an in-flight instance
"""
from __future__ import annotations

import asyncio

import pytest

from agentguard import AgentGuard, ApprovalRequiredException, ToolDeniedException

POLICY_YAML = """
version: "1.0.0"
rules:
  - name: approve_wire_transfer
    tool: wire_transfer
    action: require_approval
  - name: deny_delete
    tool: delete_customer
    action: deny
budget:
  max_tool_calls: 5
defaults:
  unmatched_tool: allow
"""


@pytest.fixture()
def policy_file(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(POLICY_YAML)
    return str(p)


@pytest.fixture()
def guard(policy_file, tmp_path):
    g = AgentGuard.from_policy(policy_file, db_path=str(tmp_path / "test.db"))
    yield g
    g._db.close_thread_connection()


class TestFromPolicy:
    def test_constructs_successfully(self, guard):
        assert guard.policy_version == "1.0.0"

    def test_missing_policy_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            AgentGuard.from_policy(str(tmp_path / "nonexistent.yaml"))


class TestDecoratorAllowPath:
    def test_allow_path_calls_real_function_and_returns_result(self, guard):
        @guard.tool
        def search_docs(query: str) -> str:
            return f"found: {query}"

        result = search_docs("invoice", run_id="run-1")
        assert result == "found: invoice"

    def test_decorator_without_parens(self, guard):
        @guard.tool
        def my_tool(x: int) -> int:
            return x * 2

        assert my_tool(5, run_id="run-1") == 10

    def test_decorator_with_parens_and_custom_run_id_param(self, guard):
        @guard.tool(run_id_param="custom_run_id")
        def my_tool(x: int) -> int:
            return x * 2

        assert my_tool(5, custom_run_id="run-1") == 10


class TestDecoratorDenyPath:
    def test_deny_raises_and_does_not_call_real_function(self, guard):
        call_log = []

        @guard.tool
        def delete_customer(customer_id: int) -> dict:
            call_log.append(customer_id)
            return {"deleted": customer_id}

        with pytest.raises(ToolDeniedException):
            delete_customer(123, run_id="run-1")

        assert call_log == []  # real function must never have run


class TestDecoratorApprovalPath:
    def test_require_approval_raises_and_does_not_call_real_function(self, guard):
        call_log = []

        @guard.tool
        def wire_transfer(account_id: str, amount: float) -> dict:
            call_log.append((account_id, amount))
            return {"status": "transferred"}

        with pytest.raises(ApprovalRequiredException):
            wire_transfer("ACC1", 100.0, run_id="run-1")

        assert call_log == []

    def test_approved_resume_calls_real_function_and_returns_result(self, guard):
        call_log = []

        @guard.tool
        def wire_transfer(account_id: str, amount: float) -> dict:
            call_log.append((account_id, amount))
            return {"status": "transferred", "account_id": account_id}

        approval_id = None
        try:
            wire_transfer("ACC1", 100.0, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id

        guard.approve(approval_id, approver="alice")

        result = wire_transfer("ACC1", 100.0, run_id="run-1")
        assert result == {"status": "transferred", "account_id": "ACC1"}
        assert call_log == [("ACC1", 100.0)]  # called exactly once

    def test_rejected_resume_raises_tool_denied(self, guard):
        @guard.tool
        def wire_transfer(account_id: str, amount: float) -> dict:
            return {"status": "transferred"}

        approval_id = None
        try:
            wire_transfer("ACC1", 100.0, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id

        guard.reject(approval_id, approver="bob")

        with pytest.raises(ToolDeniedException):
            wire_transfer("ACC1", 100.0, run_id="run-1")


class TestRunIdStripping:
    def test_run_id_does_not_reach_real_function(self, guard):
        """
        If run_id leaks through to the real function and that function's
        signature does not accept it, this raises TypeError -- proving
        the stripping is correct rather than accidentally permissive.
        """
        @guard.tool
        def strict_signature_tool(query: str) -> str:
            return query  # genuinely has no run_id parameter

        result = strict_signature_tool("test", run_id="run-1")
        assert result == "test"

    def test_works_without_run_id_at_all(self, guard):
        """Falls through to synthetic run_id generation; must not raise."""
        @guard.tool
        def my_tool(x: int) -> int:
            return x

        assert my_tool(5) == 5


class TestCallingStyleIdempotency:
    """
    Regression tests for the bug found during manual verification:
    sig.bind() was being attempted with run_id still present in kwargs,
    which raised TypeError (since the real function's signature has no
    run_id param) and fell into an inconsistent fallback path -- positional
    calls and keyword calls then produced DIFFERENT argument dicts for the
    exact same logical call, breaking approval idempotency.
    """

    def test_positional_and_keyword_calls_produce_same_approval_id(self, guard):
        @guard.tool
        def wire_transfer(account_id: str, amount: float) -> dict:
            return {"ok": True}

        id1 = None
        try:
            wire_transfer("ACC1", 100.0, run_id="run-1")
        except ApprovalRequiredException as e:
            id1 = e.approval_id

        id2 = None
        try:
            wire_transfer(account_id="ACC1", amount=100.0, run_id="run-1")
        except ApprovalRequiredException as e:
            id2 = e.approval_id

        assert id1 is not None and id2 is not None
        assert id1 == id2

    def test_mixed_positional_and_keyword_calls_produce_same_approval_id(self, guard):
        @guard.tool
        def wire_transfer(account_id: str, amount: float) -> dict:
            return {"ok": True}

        id1 = None
        try:
            wire_transfer("ACC1", 100.0, run_id="run-1")
        except ApprovalRequiredException as e:
            id1 = e.approval_id

        id2 = None
        try:
            wire_transfer("ACC1", amount=100.0, run_id="run-1")  # mixed style
        except ApprovalRequiredException as e:
            id2 = e.approval_id

        assert id1 == id2

    def test_only_one_pending_approval_exists_after_multiple_calling_styles(self, guard):
        @guard.tool
        def wire_transfer(account_id: str, amount: float) -> dict:
            return {"ok": True}

        for call in [
            lambda: wire_transfer("ACC1", 100.0, run_id="run-1"),
            lambda: wire_transfer(account_id="ACC1", amount=100.0, run_id="run-1"),
            lambda: wire_transfer("ACC1", amount=100.0, run_id="run-1"),
        ]:
            try:
                call()
            except ApprovalRequiredException:
                pass

        pending = guard.get_pending_approvals()
        assert len(pending) == 1

    def test_genuinely_different_arguments_produce_different_approval_ids(self, guard):
        @guard.tool
        def wire_transfer(account_id: str, amount: float) -> dict:
            return {"ok": True}

        id1 = None
        try:
            wire_transfer("ACC1", 100.0, run_id="run-1")
        except ApprovalRequiredException as e:
            id1 = e.approval_id

        id2 = None
        try:
            wire_transfer("ACC2", 999.0, run_id="run-1")  # different args
        except ApprovalRequiredException as e:
            id2 = e.approval_id

        assert id1 != id2


class TestAsyncSupport:
    def test_async_allow_path(self, guard):
        @guard.tool
        async def search_docs(query: str) -> list:
            await asyncio.sleep(0.001)
            return [f"result for {query}"]

        async def run():
            return await search_docs("invoice", run_id="run-1")

        result = asyncio.run(run())
        assert result == ["result for invoice"]

    def test_async_deny_path_does_not_call_real_function(self, guard):
        call_log = []

        @guard.tool
        async def delete_customer(customer_id: int) -> dict:
            call_log.append(customer_id)
            return {"deleted": customer_id}

        async def run():
            with pytest.raises(ToolDeniedException):
                await delete_customer(123, run_id="run-1")

        asyncio.run(run())
        assert call_log == []

    def test_async_approval_idempotency(self, guard):
        @guard.tool
        async def wire_transfer(account_id: str, amount: float) -> dict:
            return {"ok": True}

        async def run():
            id1 = None
            try:
                await wire_transfer("ACC1", 100.0, run_id="run-1")
            except ApprovalRequiredException as e:
                id1 = e.approval_id

            id2 = None
            try:
                await wire_transfer(account_id="ACC1", amount=100.0, run_id="run-1")
            except ApprovalRequiredException as e:
                id2 = e.approval_id

            return id1, id2

        id1, id2 = asyncio.run(run())
        assert id1 == id2


class TestWrapMethod:
    def test_wrap_works_as_alternative_to_decorator(self, guard):
        def raw_tool(x: int) -> int:
            return x + 1

        wrapped = guard.wrap(raw_tool)
        assert wrapped(5, run_id="run-1") == 6

    def test_wrap_preserves_function_metadata(self, guard):
        def raw_tool(x: int) -> int:
            """Docstring."""
            return x

        wrapped = guard.wrap(raw_tool)
        assert wrapped.__name__ == "raw_tool"
        assert wrapped.__doc__ == "Docstring."


class TestApprovalConvenienceMethods:
    def test_approve_and_get_approval(self, guard):
        @guard.tool
        def wire_transfer(account_id: str, amount: float) -> dict:
            return {"ok": True}

        approval_id = None
        try:
            wire_transfer("ACC1", 100.0, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id

        result = guard.approve(approval_id, approver="alice")
        assert result is True

        record = guard.get_approval(approval_id)
        assert record.status.value == "APPROVED"

    def test_get_approval_unknown_id_returns_none(self, guard):
        assert guard.get_approval("does-not-exist") is None


class TestRunLifecycle:
    def test_reset_run_clears_budget_state(self, guard):
        @guard.tool
        def tool_a(x: int) -> int:
            return x

        @guard.tool
        def tool_b(x: int) -> int:
            return x

        @guard.tool
        def tool_c(x: int) -> int:
            return x

        @guard.tool
        def tool_d(x: int) -> int:
            return x

        @guard.tool
        def tool_e(x: int) -> int:
            return x

        # Use 5 DISTINCT tool names to exhaust the budget of 5 without
        # also triggering loop detection (which fires on repeated calls
        # to the SAME tool name, by default after 5 repetitions too --
        # using the same tool name here would mask which subsystem
        # actually caused the denial, same issue caught earlier in
        # test_decision_engine.py).
        tool_a(1, run_id="run-1")
        tool_b(2, run_id="run-1")
        tool_c(3, run_id="run-1")
        tool_d(4, run_id="run-1")
        tool_e(5, run_id="run-1")

        with pytest.raises(ToolDeniedException) as exc_info:
            tool_a(99, run_id="run-1")  # 6th call, budget exhausted
        assert "Budget exceeded" in exc_info.value.reason

        guard.reset_run("run-1")

        # After reset, the run should be able to proceed again.
        result = tool_a(100, run_id="run-1")
        assert result == 100

    def test_reset_run_does_not_delete_audit_history(self, guard):
        @guard.tool
        def some_tool(x: int) -> int:
            return x

        some_tool(1, run_id="run-1")
        guard.reset_run("run-1")

        history = guard.get_run_audit("run-1")
        assert len(history) == 1  # the prior ALLOW record still exists


class TestPolicyReload:
    def test_reload_picks_up_new_rules(self, guard, policy_file):
        @guard.tool
        def some_new_tool(x: int) -> int:
            return x

        # Initially unmatched -> default allow.
        assert some_new_tool(1, run_id="run-1") == 1

        # Replace with a complete, valid policy document that adds a
        # deny rule for this tool, then reload. (Naively concatenating
        # strings onto POLICY_YAML is invalid here, since POLICY_YAML's
        # `rules:` list is followed by other top-level keys -- appending
        # a list item after those keys produces malformed YAML.)
        new_yaml = """
version: "1.0.0"
rules:
  - name: approve_wire_transfer
    tool: wire_transfer
    action: require_approval
  - name: deny_delete
    tool: delete_customer
    action: deny
  - name: deny_new_tool
    tool: some_new_tool
    action: deny
budget:
  max_tool_calls: 5
defaults:
  unmatched_tool: allow
"""
        with open(policy_file, "w") as f:
            f.write(new_yaml)
        guard.reload_policy()

        with pytest.raises(ToolDeniedException):
            some_new_tool(2, run_id="run-1")
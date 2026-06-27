"""
Tests for agentguard.interceptor.Interceptor.

Covers:
- ALLOW path: returns ctx, logs ALLOW
- DENY path: raises ToolDeniedException, logs DENY
- REQUIRE_APPROVAL path: creates approval, raises ApprovalRequiredException
- Idempotency: retrying the same call re-raises the SAME approval_id,
  does not create a duplicate
- Post-approval resume: returns ctx (does not raise) once APPROVED
- Post-rejection resume: raises ToolDeniedException once REJECTED
- Audit log reflects ACTUAL outcome (ALLOW/DENY), not the stale
  REQUIRE_APPROVAL label, once an approval is resolved
- runs table is upserted automatically (no FK violation on first call
  for a brand new run_id)
- Budget/loop overrides still flow through DENY correctly via Interceptor
- Concurrent calls racing to create the first approval for the same
  composite key: exactly one creates a record, the other observes it
"""
from __future__ import annotations

import threading

import pytest

from agentguard.approvals.manager import ApprovalManager
from agentguard.audit.logger import AuditLogger
from agentguard.budget.tracker import BudgetTracker
from agentguard.decisions.engine import DecisionEngine
from agentguard.exceptions import ApprovalRequiredException, ToolDeniedException
from agentguard.interceptor import Interceptor
from agentguard.loops.detector import LoopDetector
from agentguard.policy.engine import PolicyEngine
from agentguard.storage.database import DatabaseManager

POLICY_YAML = """
version: "1.0.0"
rules:
  - name: deny_delete
    tool: delete_customer
    action: deny
  - name: approve_email
    tool: send_email
    action: require_approval
budget:
  max_tool_calls: 3
loop_detection:
  max_repetitions: 3
  window_size: 6
defaults:
  unmatched_tool: allow
"""


@pytest.fixture()
def policy_file(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(POLICY_YAML)
    return str(p)


@pytest.fixture()
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    yield manager
    manager.close_thread_connection()


@pytest.fixture()
def interceptor(policy_file, db):
    pe = PolicyEngine(policy_file)
    bt = BudgetTracker()
    ld = LoopDetector(max_repetitions=3, window_size=6)
    de = DecisionEngine(pe, bt, ld)
    am = ApprovalManager(db)
    al = AuditLogger(db)
    ic = Interceptor(pe, de, am, al, db)
    # Expose subsystems on the instance for test introspection.
    ic._test_approval_manager = am
    ic._test_audit_logger = al
    return ic


class TestRunsTableUpsert:
    def test_first_call_for_new_run_id_does_not_raise_fk_violation(self, interceptor):
        # Before this call, no `runs` row exists for "brand-new-run".
        ctx = interceptor.check("search_docs", {}, run_id="brand-new-run")
        assert ctx.run_id == "brand-new-run"

    def test_second_call_for_same_run_id_does_not_raise_on_duplicate_upsert(self, interceptor):
        interceptor.check("search_docs", {}, run_id="run-1")
        interceptor.check("other_tool", {}, run_id="run-1")  # must not raise


class TestAllowPath:
    def test_allow_returns_context(self, interceptor):
        ctx = interceptor.check("search_docs", {"q": "test"}, run_id="run-1")
        assert ctx.tool_name == "search_docs"

    def test_allow_logs_audit_record(self, interceptor):
        interceptor.check("search_docs", {}, run_id="run-1")
        events = interceptor._test_audit_logger.get_by_run_id("run-1")
        assert len(events) == 1
        assert events[0].decision == "ALLOW"


class TestDenyPath:
    def test_deny_raises_tool_denied(self, interceptor):
        with pytest.raises(ToolDeniedException) as exc_info:
            interceptor.check("delete_customer", {}, run_id="run-1")
        assert exc_info.value.tool_name == "delete_customer"

    def test_deny_logs_audit_record(self, interceptor):
        with pytest.raises(ToolDeniedException):
            interceptor.check("delete_customer", {}, run_id="run-1")
        events = interceptor._test_audit_logger.get_by_run_id("run-1")
        assert len(events) == 1
        assert events[0].decision == "DENY"


class TestRequireApprovalPath:
    def test_first_call_creates_approval_and_raises(self, interceptor):
        with pytest.raises(ApprovalRequiredException) as exc_info:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        assert exc_info.value.approval_id
        assert exc_info.value.tool_name == "send_email"

        pending = interceptor._test_approval_manager.get_pending()
        assert len(pending) == 1

    def test_initial_request_logs_require_approval(self, interceptor):
        with pytest.raises(ApprovalRequiredException):
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        events = interceptor._test_audit_logger.get_by_run_id("run-1")
        assert events[0].decision == "REQUIRE_APPROVAL"
        assert events[0].approval_id is not None

    def test_retry_same_call_reraises_same_approval_id_no_duplicate(self, interceptor):
        first_id = None
        try:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        except ApprovalRequiredException as e:
            first_id = e.approval_id

        try:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        except ApprovalRequiredException as e:
            second_id = e.approval_id

        assert first_id == second_id
        pending = interceptor._test_approval_manager.get_pending()
        assert len(pending) == 1  # still only one record, not two

    def test_different_arguments_create_a_distinct_approval(self, interceptor):
        ids = []
        for recipient in ["a@x.com", "b@x.com"]:
            try:
                interceptor.check("send_email", {"to": recipient}, run_id="run-1")
            except ApprovalRequiredException as e:
                ids.append(e.approval_id)
        assert ids[0] != ids[1]
        pending = interceptor._test_approval_manager.get_pending()
        assert len(pending) == 2


class TestPostApprovalResume:
    def test_approved_call_proceeds_without_raising(self, interceptor):
        approval_id = None
        try:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id

        interceptor._test_approval_manager.approve(approval_id, approver="alice")

        ctx = interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        assert ctx.tool_name == "send_email"  # did not raise

    def test_audit_log_shows_allow_not_require_approval_after_resolution(self, interceptor):
        """
        This is the specific correctness fix: once an approval is granted
        and the call actually proceeds, the audit log must show the TRUE
        outcome (ALLOW), not the stale REQUIRE_APPROVAL classification.
        """
        approval_id = None
        try:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id

        interceptor._test_approval_manager.approve(approval_id, approver="alice")
        interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")

        events = interceptor._test_audit_logger.get_by_run_id("run-1")
        decisions = [e.decision for e in events]
        # First event: the original classification (legitimately PENDING
        # at that point in time). Second event: the actual outcome.
        assert decisions == ["REQUIRE_APPROVAL", "ALLOW"]
        assert events[1].approval_id == approval_id

    def test_count_by_decision_correctly_reflects_actual_outcomes(self, interceptor):
        """
        A reader using count_by_decision() for reporting must see an
        accurate count -- not an inflated REQUIRE_APPROVAL count that
        includes calls which actually executed.
        """
        approval_id = None
        try:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id
        interceptor._test_approval_manager.approve(approval_id, approver="alice")
        interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")

        counts = interceptor._test_audit_logger.count_by_decision()
        assert counts["ALLOW"] == 1
        assert counts["REQUIRE_APPROVAL"] == 1


class TestPostRejectionResume:
    def test_rejected_call_raises_tool_denied(self, interceptor):
        approval_id = None
        try:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id

        interceptor._test_approval_manager.reject(approval_id, approver="bob")

        with pytest.raises(ToolDeniedException):
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")

    def test_rejected_call_does_not_create_new_approval(self, interceptor):
        approval_id = None
        try:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id
        interceptor._test_approval_manager.reject(approval_id, approver="bob")

        with pytest.raises(ToolDeniedException):
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")

        # Still exactly one approval record overall -- no new PENDING
        # request was generated by the retry. get_pending() returns only
        # PENDING records, so a correctly-behaving retry leaves it empty
        # (the one record that exists is REJECTED, not pending).
        pending = interceptor._test_approval_manager.get_pending()
        assert len(pending) == 0

    def test_audit_log_shows_deny_not_require_approval_after_rejection(self, interceptor):
        approval_id = None
        try:
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")
        except ApprovalRequiredException as e:
            approval_id = e.approval_id
        interceptor._test_approval_manager.reject(approval_id, approver="bob")

        with pytest.raises(ToolDeniedException):
            interceptor.check("send_email", {"to": "x@y.com"}, run_id="run-1")

        events = interceptor._test_audit_logger.get_by_run_id("run-1")
        decisions = [e.decision for e in events]
        assert decisions == ["REQUIRE_APPROVAL", "DENY"]


class TestBudgetAndLoopFlowThroughInterceptor:
    def test_budget_exhaustion_raises_tool_denied(self, interceptor):
        interceptor.check("tool_a", {}, run_id="run-1")
        interceptor.check("tool_b", {}, run_id="run-1")
        interceptor.check("tool_c", {}, run_id="run-1")  # 3rd call, budget=3
        with pytest.raises(ToolDeniedException) as exc_info:
            interceptor.check("tool_d", {}, run_id="run-1")
        assert "Budget exceeded" in exc_info.value.reason

    def test_loop_detection_raises_tool_denied(self, interceptor):
        interceptor.check("repeated_tool", {}, run_id="run-1")
        interceptor.check("repeated_tool", {}, run_id="run-1")
        with pytest.raises(ToolDeniedException) as exc_info:
            interceptor.check("repeated_tool", {}, run_id="run-1")  # 3rd in a row
        assert "Loop detected" in exc_info.value.reason


class TestConcurrentApprovalCreationRace:
    def test_concurrent_first_calls_only_create_one_approval(self, interceptor):
        """
        Two threads call check() for the EXACT same composite key at the
        same time, both racing to be the one that creates the approval.
        The UNIQUE constraint on the approvals table (EDS 5.3) means at
        most one INSERT can succeed; the other should either: (a) hit
        the duplicate-key error path, or (b) have found the just-created
        record via find_existing() if it ran slightly after. Either way,
        exactly one approval record must exist at the end -- never two,
        never zero.
        """
        results = []
        results_lock = threading.Lock()

        def attempt():
            try:
                interceptor.check("send_email", {"to": "race@x.com"}, run_id="run-1")
                results_value = "no_exception"
            except ApprovalRequiredException as e:
                results_value = ("approval_required", e.approval_id)
            except Exception as e:  # noqa: BLE001
                results_value = ("other_exception", str(e))
            with results_lock:
                results.append(results_value)

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Regardless of how each individual call resolved, exactly one
        # approval record must exist for this composite key at the end.
        pending = interceptor._test_approval_manager.get_pending()
        matching = [
            p for p in pending
            if p.tool_name == "send_email"
        ]
        assert len(matching) == 1
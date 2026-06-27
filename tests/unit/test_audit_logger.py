"""
Tests for agentguard.audit.logger.AuditLogger.

Covers:
- log_decision() writes a complete row and returns a matching AuditRecord
- Optional fields (approval_id, budget_*, loop_count) default to None
  and are correctly persisted when supplied
- metadata dict round-trips through JSON correctly
- All query patterns from EDS 4.4.5: by run_id, by decision, by tool_name,
  approval events for a run, decision counts
- Immutability-by-absence: AuditLogger has no update/delete method at all
- Insertion order is preserved in query results
"""
from __future__ import annotations

import pytest

from agentguard.audit.logger import AuditLogger
from agentguard.context import ExecutionContext
from agentguard.decisions.models import DecisionType, PolicyDecision
from agentguard.storage.database import DatabaseManager


@pytest.fixture()
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.execute(
        "INSERT INTO runs (run_id, created_at, framework) VALUES (?, ?, ?)",
        ("run-1", "2026-01-01T00:00:00+00:00", "test"),
    )
    manager.execute(
        "INSERT INTO runs (run_id, created_at, framework) VALUES (?, ?, ?)",
        ("run-2", "2026-01-01T00:00:00+00:00", "test"),
    )
    yield manager
    manager.close_thread_connection()


@pytest.fixture()
def logger(db):
    return AuditLogger(db)


def make_ctx(tool_name="wire_transfer", run_id="run-1", arguments=None, **kwargs):
    return ExecutionContext.build(
        tool_name=tool_name,
        arguments=arguments or {},
        policy_version="1.0.0",
        run_id=run_id,
        **kwargs,
    )


def make_decision(decision_type=DecisionType.ALLOW, **kwargs):
    defaults = dict(decision=decision_type, reason="test reason", policy_version="1.0.0")
    defaults.update(kwargs)
    return PolicyDecision(**defaults)


class TestLogDecision:
    def test_logs_a_complete_record(self, logger):
        ctx = make_ctx(framework="langgraph")
        decision = make_decision(DecisionType.DENY, rule_matched="deny_rule")
        record = logger.log_decision(ctx, decision)

        assert record.id is not None
        assert record.run_id == "run-1"
        assert record.tool_name == "wire_transfer"
        assert record.decision == "DENY"
        assert record.rule_matched == "deny_rule"
        assert record.framework == "langgraph"
        assert record.policy_version == "1.0.0"

    def test_assigns_increasing_ids(self, logger):
        ctx = make_ctx()
        decision = make_decision()
        r1 = logger.log_decision(ctx, decision)
        r2 = logger.log_decision(ctx, decision)
        assert r2.id > r1.id

    def test_optional_fields_default_to_none(self, logger):
        ctx = make_ctx()
        decision = make_decision()
        record = logger.log_decision(ctx, decision)
        assert record.approval_id is None
        assert record.budget_calls_used is None
        assert record.budget_cost_used is None
        assert record.loop_count is None

    def test_optional_fields_persisted_when_supplied(self, logger, db):
        # approval_id is a real foreign key into the approvals table
        # (schema: `approval_id TEXT REFERENCES approvals(approval_id)`),
        # so this test must create a real approval row first rather than
        # passing a fictional ID -- PRAGMA foreign_keys=ON (set by
        # DatabaseManager) correctly rejects a dangling reference.
        from agentguard.approvals.manager import ApprovalManager

        approval_mgr = ApprovalManager(db)
        approval = approval_mgr.create_approval("run-1", "wire_transfer", "somehash", "1.0.0")

        ctx = make_ctx()
        decision = make_decision(DecisionType.REQUIRE_APPROVAL)
        record = logger.log_decision(
            ctx,
            decision,
            approval_id=approval.approval_id,
            budget_calls_used=5,
            budget_cost_used=2.5,
            loop_count=3,
        )
        assert record.approval_id == approval.approval_id
        assert record.budget_calls_used == 5
        assert record.budget_cost_used == 2.5
        assert record.loop_count == 3

        # Confirm it round-trips through a fresh read, not just the
        # in-memory return value from log_decision() itself.
        fetched = logger.get_by_run_id(ctx.run_id)[0]
        assert fetched.approval_id == approval.approval_id
        assert fetched.budget_calls_used == 5

    def test_metadata_round_trips_through_json(self, logger):
        ctx = make_ctx(metadata={"source": "test_suite", "attempt": 2})
        decision = make_decision()
        logger.log_decision(ctx, decision)

        fetched = logger.get_by_run_id(ctx.run_id)[0]
        assert fetched.metadata == {"source": "test_suite", "attempt": 2}

    def test_empty_metadata_persisted_as_none(self, logger):
        ctx = make_ctx()  # no metadata passed -> defaults to {}
        decision = make_decision()
        logger.log_decision(ctx, decision)

        fetched = logger.get_by_run_id(ctx.run_id)[0]
        assert fetched.metadata is None

    def test_arguments_hash_is_persisted(self, logger):
        ctx = make_ctx(arguments={"amount": 500})
        decision = make_decision()
        record = logger.log_decision(ctx, decision)
        assert record.arguments_hash == ctx.arguments_hash
        assert len(record.arguments_hash) == 64

    def test_dangling_approval_id_raises_agentguard_error(self, logger):
        """
        approval_id is a real foreign key into the approvals table. A
        fictional/nonexistent approval_id must raise AgentGuardDatabaseError
        (AgentGuard's own exception type), not a raw sqlite3.IntegrityError.
        """
        from agentguard.exceptions import AgentGuardDatabaseError

        ctx = make_ctx()
        decision = make_decision(DecisionType.REQUIRE_APPROVAL)
        with pytest.raises(AgentGuardDatabaseError):
            logger.log_decision(ctx, decision, approval_id="does-not-exist")


class TestQueryByRunId:
    def test_returns_only_matching_run(self, logger):
        logger.log_decision(make_ctx(run_id="run-1"), make_decision())
        logger.log_decision(make_ctx(run_id="run-2"), make_decision())
        logger.log_decision(make_ctx(run_id="run-1"), make_decision())

        events = logger.get_by_run_id("run-1")
        assert len(events) == 2
        assert all(e.run_id == "run-1" for e in events)

    def test_returns_empty_list_for_unknown_run(self, logger):
        assert logger.get_by_run_id("never-existed") == []

    def test_preserves_insertion_order(self, logger):
        d1 = make_decision(DecisionType.ALLOW, reason="first")
        d2 = make_decision(DecisionType.DENY, reason="second")
        d3 = make_decision(DecisionType.ALLOW, reason="third")
        logger.log_decision(make_ctx(tool_name="tool_a"), d1)
        logger.log_decision(make_ctx(tool_name="tool_b"), d2)
        logger.log_decision(make_ctx(tool_name="tool_c"), d3)

        events = logger.get_by_run_id("run-1")
        assert [e.reason for e in events] == ["first", "second", "third"]


class TestQueryByDecision:
    def test_filters_by_decision_type(self, logger):
        logger.log_decision(make_ctx(), make_decision(DecisionType.ALLOW))
        logger.log_decision(make_ctx(), make_decision(DecisionType.DENY))
        logger.log_decision(make_ctx(), make_decision(DecisionType.DENY))

        denies = logger.get_by_decision("DENY")
        assert len(denies) == 2
        assert all(d.decision == "DENY" for d in denies)

    def test_since_filter_excludes_older_records(self, logger):
        logger.log_decision(make_ctx(), make_decision(DecisionType.DENY))
        # Use a far-future "since" to guarantee the existing record is excluded.
        future = "2099-01-01T00:00:00+00:00"
        results = logger.get_by_decision("DENY", since=future)
        assert results == []

    def test_since_filter_includes_matching_records(self, logger):
        logger.log_decision(make_ctx(), make_decision(DecisionType.DENY))
        past = "2000-01-01T00:00:00+00:00"
        results = logger.get_by_decision("DENY", since=past)
        assert len(results) == 1


class TestQueryByToolName:
    def test_filters_by_tool_name(self, logger):
        logger.log_decision(make_ctx(tool_name="wire_transfer"), make_decision())
        logger.log_decision(make_ctx(tool_name="send_email"), make_decision())
        logger.log_decision(make_ctx(tool_name="wire_transfer"), make_decision())

        results = logger.get_by_tool_name("wire_transfer")
        assert len(results) == 2
        assert all(r.tool_name == "wire_transfer" for r in results)

    def test_unknown_tool_returns_empty(self, logger):
        assert logger.get_by_tool_name("never_called") == []


class TestApprovalEventsForRun:
    def test_returns_only_require_approval_events(self, logger):
        logger.log_decision(make_ctx(run_id="run-1"), make_decision(DecisionType.ALLOW))
        logger.log_decision(make_ctx(run_id="run-1"), make_decision(DecisionType.REQUIRE_APPROVAL))
        logger.log_decision(make_ctx(run_id="run-1"), make_decision(DecisionType.DENY))

        events = logger.get_approval_events_for_run("run-1")
        assert len(events) == 1
        assert events[0].decision == "REQUIRE_APPROVAL"

    def test_excludes_other_runs(self, logger):
        logger.log_decision(make_ctx(run_id="run-2"), make_decision(DecisionType.REQUIRE_APPROVAL))
        events = logger.get_approval_events_for_run("run-1")
        assert events == []


class TestCountByDecision:
    def test_counts_grouped_correctly(self, logger):
        logger.log_decision(make_ctx(), make_decision(DecisionType.ALLOW))
        logger.log_decision(make_ctx(), make_decision(DecisionType.ALLOW))
        logger.log_decision(make_ctx(), make_decision(DecisionType.DENY))
        logger.log_decision(make_ctx(), make_decision(DecisionType.REQUIRE_APPROVAL))

        counts = logger.count_by_decision()
        assert counts == {"ALLOW": 2, "DENY": 1, "REQUIRE_APPROVAL": 1}

    def test_empty_log_returns_empty_dict(self, logger):
        assert logger.count_by_decision() == {}


class TestImmutabilityByAbsence:
    def test_no_update_method_exists(self, logger):
        assert not hasattr(logger, "update")
        assert not hasattr(logger, "update_decision")
        assert not hasattr(logger, "update_record")

    def test_no_delete_method_exists(self, logger):
        assert not hasattr(logger, "delete")
        assert not hasattr(logger, "delete_decision")
        assert not hasattr(logger, "delete_record")
        assert not hasattr(logger, "clear")

    def test_public_surface_is_write_once_read_many(self, logger):
        """
        Enumerate the actual public methods and confirm none of them
        could plausibly mutate or remove an existing row. This is a
        structural guard against accidentally adding a mutating method
        in the future without it being caught by this test.
        """
        public_methods = [
            name for name in dir(logger)
            if not name.startswith("_") and callable(getattr(logger, name))
        ]
        forbidden_prefixes = ("update", "delete", "remove", "clear", "drop", "truncate")
        violations = [
            m for m in public_methods if m.lower().startswith(forbidden_prefixes)
        ]
        assert violations == [], f"Found mutating-sounding methods: {violations}"
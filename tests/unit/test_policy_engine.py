"""
Tests for agentguard.policy.engine.PolicyEngine.

Covers:
- Loading and constructing from a real YAML file on disk
- Evaluation: first-match-wins, exact match, default fallback
- Hot-reload: atomic swap, failed reload keeps old policy active
- File not found / malformed file handling
"""
from __future__ import annotations

import threading
import time

import pytest

from agentguard.context import ExecutionContext
from agentguard.decisions.models import DecisionType
from agentguard.exceptions import PolicyValidationError
from agentguard.policy.engine import PolicyEngine

VALID_POLICY_YAML = """
version: "1.0.0"
description: "test policy"
rules:
  - name: deny_delete_customer
    tool: delete_customer
    action: deny
    reason: "Deleting customers is never permitted"
  - name: approve_send_email
    tool: send_email
    action: require_approval
    reason: "All outbound email requires review"
  - name: allow_read_customer
    tool: read_customer
    action: allow
    reason: "Read-only operations are unrestricted"
budget:
  max_tool_calls: 10
loop_detection:
  max_repetitions: 3
  window_size: 6
defaults:
  unmatched_tool: allow
"""

MALFORMED_POLICY_YAML = """
version: ""
rules: "not-a-list"
"""

INVALID_YAML_SYNTAX = """
version: "1.0.0
rules: [
"""


def make_ctx(tool_name: str, arguments: dict | None = None, policy_version: str = "1.0.0"):
    return ExecutionContext.build(
        tool_name=tool_name,
        arguments=arguments or {},
        policy_version=policy_version,
        run_id="test-run",
    )


@pytest.fixture()
def policy_file(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(VALID_POLICY_YAML)
    return str(p)


class TestPolicyLoading:
    def test_loads_valid_policy(self, policy_file):
        engine = PolicyEngine(policy_file)
        assert engine.policy_version == "1.0.0"
        assert len(engine.policy.rules) == 3

    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            PolicyEngine(str(tmp_path / "does_not_exist.yaml"))

    def test_malformed_policy_raises_validation_error(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text(MALFORMED_POLICY_YAML)
        with pytest.raises(PolicyValidationError):
            PolicyEngine(str(p))

    def test_invalid_yaml_syntax_raises_validation_error(self, tmp_path):
        p = tmp_path / "syntax_error.yaml"
        p.write_text(INVALID_YAML_SYNTAX)
        with pytest.raises(PolicyValidationError):
            PolicyEngine(str(p))


class TestEvaluation:
    def test_exact_match_deny(self, policy_file):
        engine = PolicyEngine(policy_file)
        decision = engine.evaluate(make_ctx("delete_customer"))
        assert decision.decision == DecisionType.DENY
        assert decision.rule_matched == "deny_delete_customer"

    def test_exact_match_require_approval(self, policy_file):
        engine = PolicyEngine(policy_file)
        decision = engine.evaluate(make_ctx("send_email"))
        assert decision.decision == DecisionType.REQUIRE_APPROVAL
        assert decision.rule_matched == "approve_send_email"

    def test_exact_match_allow(self, policy_file):
        engine = PolicyEngine(policy_file)
        decision = engine.evaluate(make_ctx("read_customer"))
        assert decision.decision == DecisionType.ALLOW

    def test_no_match_falls_back_to_default(self, policy_file):
        engine = PolicyEngine(policy_file)
        decision = engine.evaluate(make_ctx("totally_unknown_tool"))
        assert decision.decision == DecisionType.ALLOW
        assert decision.rule_matched is None
        assert "totally_unknown_tool" in decision.reason

    def test_decision_carries_policy_version(self, policy_file):
        engine = PolicyEngine(policy_file)
        decision = engine.evaluate(make_ctx("read_customer"))
        assert decision.policy_version == "1.0.0"

    def test_case_sensitive_matching(self, policy_file):
        engine = PolicyEngine(policy_file)
        # "Delete_Customer" != "delete_customer" -- must fall to default.
        decision = engine.evaluate(make_ctx("Delete_Customer"))
        assert decision.decision == DecisionType.ALLOW  # default, not the deny rule
        assert decision.rule_matched is None

    def test_default_deny_policy(self, tmp_path):
        p = tmp_path / "deny_default.yaml"
        p.write_text("""
version: "1.0.0"
rules: []
defaults:
  unmatched_tool: deny
""")
        engine = PolicyEngine(str(p))
        decision = engine.evaluate(make_ctx("anything"))
        assert decision.decision == DecisionType.DENY


class TestHotReload:
    def test_reload_picks_up_new_rules(self, policy_file, tmp_path):
        engine = PolicyEngine(policy_file)
        assert engine.evaluate(make_ctx("read_customer")).decision == DecisionType.ALLOW

        # Overwrite the file with read_customer now denied.
        new_yaml = VALID_POLICY_YAML.replace(
            "    action: allow\n    reason: \"Read-only operations are unrestricted\"",
            "    action: deny\n    reason: \"now denied\"",
        )
        with open(policy_file, "w") as f:
            f.write(new_yaml)

        engine.reload()
        assert engine.evaluate(make_ctx("read_customer")).decision == DecisionType.DENY

    def test_failed_reload_keeps_old_policy_active(self, policy_file):
        engine = PolicyEngine(policy_file)
        original_version = engine.policy_version

        with open(policy_file, "w") as f:
            f.write(MALFORMED_POLICY_YAML)

        with pytest.raises(PolicyValidationError):
            engine.reload()

        # Old policy must still be in effect.
        assert engine.policy_version == original_version
        decision = engine.evaluate(make_ctx("delete_customer"))
        assert decision.decision == DecisionType.DENY

    def test_reload_is_atomic_under_concurrent_evaluation(self, policy_file):
        """
        Spam evaluate() from multiple threads while reload() happens.
        No thread should observe a torn/partial policy state, and no
        exception should propagate from evaluate() itself.
        """
        engine = PolicyEngine(policy_file)
        stop = threading.Event()
        errors = []

        def evaluator():
            while not stop.is_set():
                try:
                    engine.evaluate(make_ctx("read_customer"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=evaluator) for _ in range(4)]
        for t in threads:
            t.start()

        for _ in range(20):
            engine.reload()
            time.sleep(0.001)

        stop.set()
        for t in threads:
            t.join()

        assert errors == []


class TestPolicyVersionPropertyThreadSafety:
    def test_policy_property_returns_snapshot(self, policy_file):
        engine = PolicyEngine(policy_file)
        snapshot = engine.policy
        assert snapshot.version == "1.0.0"
        assert len(snapshot.rules) == 3
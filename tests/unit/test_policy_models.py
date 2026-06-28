"""
Tests for agentguard.policy.models.PolicyConfig.from_dict.

"""
from __future__ import annotations

import pytest

from agentguard.exceptions import PolicyValidationError
from agentguard.policy.models import PolicyConfig


def minimal_valid_dict(**overrides):
    base = {
        "version": "1.0.0",
        "rules": [
            {"name": "r1", "tool": "send_email", "action": "require_approval"},
        ],
    }
    base.update(overrides)
    return base


class TestValidPolicy:
    def test_minimal_valid_policy_parses(self):
        cfg = PolicyConfig.from_dict(minimal_valid_dict())
        assert cfg.version == "1.0.0"
        assert len(cfg.rules) == 1
        assert cfg.rules[0].tool == "send_email"

    def test_defaults_applied_when_sections_absent(self):
        cfg = PolicyConfig.from_dict({"version": "1.0.0", "rules": []})
        assert cfg.budget.max_tool_calls is None
        assert cfg.loop_detection.enabled is True
        assert cfg.loop_detection.max_repetitions == 5
        assert cfg.loop_detection.window_size == 20
        assert cfg.defaults.unmatched_tool == "allow"

    def test_empty_rules_list_is_valid(self):
        cfg = PolicyConfig.from_dict({"version": "1.0.0", "rules": []})
        assert cfg.rules == []

    def test_full_policy_with_all_sections(self):
        raw = {
            "version": "2.1.0",
            "description": "prod policy",
            "rules": [
                {"name": "deny_delete", "tool": "delete_customer", "action": "deny",
                 "reason": "never allowed"},
                {"name": "approve_wire", "tool": "wire_transfer", "action": "require_approval"},
            ],
            "budget": {"max_tool_calls": 50, "max_estimated_cost": 5.0},
            "loop_detection": {"enabled": True, "max_repetitions": 3, "window_size": 10},
            "defaults": {"unmatched_tool": "deny"},
        }
        cfg = PolicyConfig.from_dict(raw)
        assert cfg.budget.max_tool_calls == 50
        assert cfg.budget.max_estimated_cost == 5.0
        assert cfg.loop_detection.max_repetitions == 3
        assert cfg.defaults.unmatched_tool == "deny"


class TestRule1_VersionRequired:
    def test_missing_version_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict({"rules": []})

    def test_empty_string_version_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict({"version": "", "rules": []})

    def test_non_string_version_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict({"version": 1.0, "rules": []})


class TestRule2_RulesMustBeList:
    def test_rules_as_dict_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict({"version": "1.0.0", "rules": {}})

    def test_rules_as_string_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict({"version": "1.0.0", "rules": "nope"})


class TestRule3_RuleFieldsRequired:
    def test_rule_missing_name_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(rules=[
                {"tool": "x", "action": "allow"}
            ]))

    def test_rule_missing_tool_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(rules=[
                {"name": "r1", "action": "allow"}
            ]))

    def test_rule_invalid_action_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(rules=[
                {"name": "r1", "tool": "x", "action": "maybe"}
            ]))

    def test_rule_not_a_dict_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(rules=["not-a-dict"]))


class TestRule4_UniqueRuleNames:
    def test_duplicate_rule_names_raise(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(rules=[
                {"name": "dup", "tool": "a", "action": "allow"},
                {"name": "dup", "tool": "b", "action": "deny"},
            ]))


class TestRule5_UniqueToolNames:
    def test_duplicate_tool_names_raise(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(rules=[
                {"name": "r1", "tool": "same_tool", "action": "allow"},
                {"name": "r2", "tool": "same_tool", "action": "deny"},
            ]))


class TestRule6_MaxToolCalls:
    def test_negative_max_tool_calls_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(budget={"max_tool_calls": -1}))

    def test_zero_max_tool_calls_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(budget={"max_tool_calls": 0}))

    def test_float_max_tool_calls_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(budget={"max_tool_calls": 1.5}))

    def test_bool_max_tool_calls_raises(self):
        # bool is a subclass of int in Python; must be explicitly rejected.
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(budget={"max_tool_calls": True}))

    def test_valid_max_tool_calls_accepted(self):
        cfg = PolicyConfig.from_dict(minimal_valid_dict(budget={"max_tool_calls": 50}))
        assert cfg.budget.max_tool_calls == 50


class TestRule7_MaxEstimatedCost:
    def test_negative_cost_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(budget={"max_estimated_cost": -1.0}))

    def test_zero_cost_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(budget={"max_estimated_cost": 0}))

    def test_valid_cost_accepted(self):
        cfg = PolicyConfig.from_dict(minimal_valid_dict(budget={"max_estimated_cost": 5.0}))
        assert cfg.budget.max_estimated_cost == 5.0

    def test_int_cost_coerced_to_float(self):
        cfg = PolicyConfig.from_dict(minimal_valid_dict(budget={"max_estimated_cost": 5}))
        assert cfg.budget.max_estimated_cost == 5.0
        assert isinstance(cfg.budget.max_estimated_cost, float)


class TestRule8_MaxRepetitions:
    def test_max_repetitions_below_2_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(
                loop_detection={"max_repetitions": 1}
            ))

    def test_max_repetitions_of_2_is_valid(self):
        cfg = PolicyConfig.from_dict(minimal_valid_dict(
            loop_detection={"max_repetitions": 2, "window_size": 4}
        ))
        assert cfg.loop_detection.max_repetitions == 2


class TestRule9_WindowSize:
    def test_window_size_below_max_repetitions_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(
                loop_detection={"max_repetitions": 5, "window_size": 3}
            ))

    def test_window_size_equal_to_max_repetitions_is_valid(self):
        cfg = PolicyConfig.from_dict(minimal_valid_dict(
            loop_detection={"max_repetitions": 5, "window_size": 5}
        ))
        assert cfg.loop_detection.window_size == 5


class TestRule10_DefaultsUnmatchedTool:
    def test_invalid_default_action_raises(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(minimal_valid_dict(
                defaults={"unmatched_tool": "require_approval"}
            ))

    def test_deny_default_is_valid(self):
        cfg = PolicyConfig.from_dict(minimal_valid_dict(
            defaults={"unmatched_tool": "deny"}
        ))
        assert cfg.defaults.unmatched_tool == "deny"


class TestMalformedPolicyErrorMessages:
    def test_error_includes_path_field_and_values(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            PolicyConfig.from_dict({"version": "", "rules": []}, source_path="/tmp/bad.yaml")
        err = exc_info.value
        assert err.path == "/tmp/bad.yaml"
        assert err.field == "version"
        assert "/tmp/bad.yaml" in str(err)
        assert "version" in str(err)

    def test_root_not_a_dict_raises_clear_error(self):
        with pytest.raises(PolicyValidationError):
            PolicyConfig.from_dict(["not", "a", "dict"])
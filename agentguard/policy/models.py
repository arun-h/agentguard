"""
Policy configuration data models and validation.

Reference:
- EDS §5.2.1 — Policy Engine: Policy File Format
- EDS §5.2.2 — Policy Engine: Validation Rules
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agentguard.exceptions import PolicyValidationError

VALID_ACTIONS = {"allow", "deny", "require_approval"}
VALID_DEFAULT_ACTIONS = {"allow", "deny"}


@dataclass
class Rule:
    name: str
    tool: str
    action: str
    reason: str = ""


@dataclass
class BudgetConfig:
    max_tool_calls: Optional[int] = None
    max_estimated_cost: Optional[float] = None


@dataclass
class LoopDetectionConfig:
    enabled: bool = True
    max_repetitions: int = 5
    window_size: int = 20


@dataclass
class DefaultsConfig:
    unmatched_tool: str = "allow"


@dataclass
class PolicyConfig:
    version: str
    rules: List[Rule] = field(default_factory=list)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    loop_detection: LoopDetectionConfig = field(default_factory=LoopDetectionConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    description: str = ""

    @classmethod
    def from_dict(cls, raw: Any, source_path: str = "<unknown>") -> "PolicyConfig":
        """
        Validate and construct a PolicyConfig from a parsed YAML dict.

        Validation order follows EDS Section 4.2.3 exactly. The first
        failure raises PolicyValidationError and halts construction.
        """
        if not isinstance(raw, dict):
            raise PolicyValidationError(
                source_path, "root", "a mapping/dict", type(raw).__name__
            )

        # 1. version must be present and a non-empty string.
        version = raw.get("version")
        if not isinstance(version, str) or not version.strip():
            raise PolicyValidationError(
                source_path, "version", "non-empty semver string", repr(version)
            )

        # 2. rules must be a list (may be empty).
        raw_rules = raw.get("rules", [])
        if not isinstance(raw_rules, list):
            raise PolicyValidationError(
                source_path, "rules", "a list", type(raw_rules).__name__
            )

        rules: List[Rule] = []
        seen_names = set()
        seen_tools = set()
        for i, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, dict):
                raise PolicyValidationError(
                    source_path, f"rules[{i}]", "a mapping/dict", type(raw_rule).__name__
                )

            # 3. Each rule must have name, tool, action.
            name = raw_rule.get("name")
            if not isinstance(name, str) or not name.strip():
                raise PolicyValidationError(
                    source_path, f"rules[{i}].name", "non-empty string", repr(name)
                )

            tool = raw_rule.get("tool")
            if not isinstance(tool, str) or not tool.strip():
                raise PolicyValidationError(
                    source_path, f"rules[{i}].tool", "non-empty string", repr(tool)
                )

            action = raw_rule.get("action")
            if action not in VALID_ACTIONS:
                raise PolicyValidationError(
                    source_path,
                    f"rules[{i}].action",
                    f"one of {sorted(VALID_ACTIONS)}",
                    repr(action),
                )

            # 4. Rule names must be unique.
            if name in seen_names:
                raise PolicyValidationError(
                    source_path, f"rules[{i}].name", "unique rule name", repr(name)
                )
            seen_names.add(name)

            # 5. Tool names within rules must be unique (first match wins
            #    semantics require no ambiguity from duplicate entries).
            if tool in seen_tools:
                raise PolicyValidationError(
                    source_path,
                    f"rules[{i}].tool",
                    "unique tool name across rules",
                    repr(tool),
                )
            seen_tools.add(tool)

            reason = raw_rule.get("reason", "")
            if reason is not None and not isinstance(reason, str):
                raise PolicyValidationError(
                    source_path, f"rules[{i}].reason", "string", repr(reason)
                )

            rules.append(Rule(name=name, tool=tool, action=action, reason=reason or ""))

        # 6 & 7. budget.max_tool_calls / max_estimated_cost validation.
        raw_budget = raw.get("budget") or {}
        if not isinstance(raw_budget, dict):
            raise PolicyValidationError(
                source_path, "budget", "a mapping/dict", type(raw_budget).__name__
            )

        max_tool_calls = raw_budget.get("max_tool_calls")
        if max_tool_calls is not None:
            if not isinstance(max_tool_calls, int) or isinstance(max_tool_calls, bool) or max_tool_calls <= 0:
                raise PolicyValidationError(
                    source_path,
                    "budget.max_tool_calls",
                    "positive integer",
                    repr(max_tool_calls),
                )

        max_estimated_cost = raw_budget.get("max_estimated_cost")
        if max_estimated_cost is not None:
            if not isinstance(max_estimated_cost, (int, float)) or isinstance(
                max_estimated_cost, bool
            ) or max_estimated_cost <= 0:
                raise PolicyValidationError(
                    source_path,
                    "budget.max_estimated_cost",
                    "positive float",
                    repr(max_estimated_cost),
                )

        budget = BudgetConfig(
            max_tool_calls=max_tool_calls,
            max_estimated_cost=float(max_estimated_cost) if max_estimated_cost is not None else None,
        )

        # 8, 9. loop_detection.max_repetitions / window_size validation.
        raw_loop = raw.get("loop_detection") or {}
        if not isinstance(raw_loop, dict):
            raise PolicyValidationError(
                source_path, "loop_detection", "a mapping/dict", type(raw_loop).__name__
            )

        loop_enabled = raw_loop.get("enabled", True)
        if not isinstance(loop_enabled, bool):
            raise PolicyValidationError(
                source_path, "loop_detection.enabled", "boolean", repr(loop_enabled)
            )

        max_repetitions = raw_loop.get("max_repetitions", 5)
        if not isinstance(max_repetitions, int) or isinstance(max_repetitions, bool) or max_repetitions < 2:
            raise PolicyValidationError(
                source_path,
                "loop_detection.max_repetitions",
                "integer >= 2",
                repr(max_repetitions),
            )

        window_size = raw_loop.get("window_size", 20)
        if not isinstance(window_size, int) or isinstance(window_size, bool) or window_size < max_repetitions:
            raise PolicyValidationError(
                source_path,
                "loop_detection.window_size",
                f"integer >= max_repetitions ({max_repetitions})",
                repr(window_size),
            )

        loop_detection = LoopDetectionConfig(
            enabled=loop_enabled,
            max_repetitions=max_repetitions,
            window_size=window_size,
        )

        # 10. defaults.unmatched_tool validation.
        raw_defaults = raw.get("defaults") or {}
        if not isinstance(raw_defaults, dict):
            raise PolicyValidationError(
                source_path, "defaults", "a mapping/dict", type(raw_defaults).__name__
            )

        unmatched_tool = raw_defaults.get("unmatched_tool", "allow")
        if unmatched_tool not in VALID_DEFAULT_ACTIONS:
            raise PolicyValidationError(
                source_path,
                "defaults.unmatched_tool",
                f"one of {sorted(VALID_DEFAULT_ACTIONS)}",
                repr(unmatched_tool),
            )

        defaults = DefaultsConfig(unmatched_tool=unmatched_tool)

        description = raw.get("description", "")
        if description is not None and not isinstance(description, str):
            raise PolicyValidationError(
                source_path, "description", "string", repr(description)
            )

        return cls(
            version=version,
            rules=rules,
            budget=budget,
            loop_detection=loop_detection,
            defaults=defaults,
            description=description or "",
        )
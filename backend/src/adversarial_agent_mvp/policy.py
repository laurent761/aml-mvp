from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .contracts import Decision, EffectAttempt, PolicyDecision, PolicyDocument


class PolicyEvaluationError(ValueError):
    pass


def _resolve(context: Mapping[str, Any], path: str) -> Any:
    value: Any = context
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


class PolicyEngine:
    """Evaluator for one immutable, episode-pinned policy snapshot."""

    _comparators = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "contains", "matches_label"}

    def __init__(self, policies: list[PolicyDocument] | None = None, capsule_mode: bool = True):
        self._policies = tuple(policies or ())
        self.capsule_mode = capsule_mode

    def evaluate(
        self, effect: EffectAttempt, virtual_state: Mapping[str, Any] | None = None
    ) -> PolicyDecision:
        context = effect.model_dump(mode="json")
        context["virtual_state"] = dict(virtual_state or {})
        for policy in self._policies:
            if self._eval_expr(policy.when.model_dump(mode="json"), context):
                if self.capsule_mode and policy.decision == Decision.ALLOW_REAL:
                    raise PolicyEvaluationError("ALLOW_REAL is invalid in capsule mode")
                return PolicyDecision(
                    decision=policy.decision,
                    policy_ids=[policy.policy_id],
                    transformed_arguments=policy.transform,
                    reason_code=policy.reason_code,
                )
        return PolicyDecision(
            decision=Decision.SIMULATE if self.capsule_mode else Decision.ALLOW_REAL,
            policy_ids=[],
            reason_code="CAPSULE_DEFAULT_SIMULATE" if self.capsule_mode else "NO_POLICY_MATCH",
        )

    def _eval_expr(self, expr: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
        if len(expr) != 1:
            raise PolicyEvaluationError("each expression must contain exactly one operator")
        operator, value = next(iter(expr.items()))
        if operator == "all":
            return all(self._eval_expr(item, context) for item in self._require_list(value))
        if operator == "any":
            return any(self._eval_expr(item, context) for item in self._require_list(value))
        if operator == "not":
            if not isinstance(value, Mapping):
                raise PolicyEvaluationError("not expects an expression")
            return not self._eval_expr(value, context)
        if operator not in self._comparators or not isinstance(value, Mapping):
            raise PolicyEvaluationError(f"unsupported policy operator: {operator}")
        actual = _resolve(context, str(value.get("field", "")))
        expected = value.get("value")
        try:
            if operator == "eq":
                return actual == expected
            if operator == "neq":
                return actual != expected
            if operator == "gt":
                return actual > expected
            if operator == "gte":
                return actual >= expected
            if operator == "lt":
                return actual < expected
            if operator == "lte":
                return actual <= expected
            if operator == "in":
                return actual in expected
            if operator == "contains":
                return expected in actual
            if operator == "matches_label":
                labels = actual if isinstance(actual, list) else [actual]
                return any(re.fullmatch(str(expected), str(label)) for label in labels)
        except (TypeError, ValueError):
            return False
        raise PolicyEvaluationError(f"unsupported policy operator: {operator}")

    @staticmethod
    def _require_list(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            raise PolicyEvaluationError("logical operator expects a list of expressions")
        return value

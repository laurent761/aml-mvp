"""Presentation provenance; never infer a free model call from a zero cost."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def invocation_usage(row: Any) -> dict[str, Any]:
    config = row.configuration or {}
    audit = config.get("target_inference") or {}
    source = audit.get("usage_source", config.get("usage_source", "unknown"))
    kind = config.get("execution_kind", "unknown")
    if row.provider == "internal" and row.model in {"HeuristicBaselineModel", "StaticAttackSuiteModel"}:
        kind, source = "simulated", "simulated"
    elif audit or row.provider in {"hosted_openai_compatible", "local_openai_compatible", "openai-compatible", "aml.attacker.v1"}:
        kind = "model"
    if source == "not_dispatched":
        kind = "not_dispatched"
    cost_source = audit.get("cost_source", config.get("cost_source", "unknown"))
    if kind == "simulated":
        cost_source = "not_applicable"
    # Historical positive charges prove some pricing existed, but zero proves nothing.
    elif cost_source == "unknown" and row.cost > 0:
        cost_source = "operator_rates"
    calls = max(1, int(config.get("successful_calls", 0)) + int(config.get("failed_calls", 0)))
    return {"execution_kind": kind, "usage_source": source, "cost_source": cost_source,
            "calls": calls, "tokens": row.tokens, "cost": row.cost}


def summarize_usage(rows: Iterable[Any]) -> dict[str, Any]:
    result: dict[str, Any] = dict(real_calls=0, simulated_calls=0, unknown_calls=0,
        provider_tokens=0, runtime_tokens=0, estimated_tokens=0, simulated_tokens=0, unknown_tokens=0,
        priced_cost=0.0, unpriced_calls=0)
    for row in rows:
        usage = invocation_usage(row)
        kind, source, calls = usage["execution_kind"], usage["usage_source"], usage["calls"]
        if kind == "not_dispatched":
            continue
        group = "simulated" if kind == "simulated" else "real" if kind == "model" else "unknown"
        result[group + "_calls"] += calls
        token_group = ("simulated" if kind == "simulated" else "provider" if source == "provider"
                       else "runtime" if source == "runtime_reported"
                       else "estimated" if source in {"estimated", "conservative_reservation", "mixed"} else "unknown")
        result[token_group + "_tokens"] += usage["tokens"]
        if kind != "simulated":
            if usage["cost_source"] == "operator_rates":
                result["priced_cost"] += usage["cost"]
            else:
                result["unpriced_calls"] += calls
    kinds = [key for key in ("real", "simulated", "unknown") if result[key + "_calls"]]
    result["execution_kind"] = kinds[0] if len(kinds) == 1 else "mixed" if kinds else "none"
    return result

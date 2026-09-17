from types import SimpleNamespace

from adversarial_agent_mvp.models import ModelUsage, OpenAICompatibleAttackerModel
from adversarial_agent_mvp.usage_reporting import invocation_usage, summarize_usage


def invocation(*, provider="hosted_openai_compatible", model="model", tokens=100, cost=0, **config):
    return SimpleNamespace(provider=provider, model=model, tokens=tokens, cost=cost, configuration=config)


def test_legacy_heuristic_is_simulation_but_unknown_internal_model_is_not():
    sim = invocation(provider="internal", model="HeuristicBaselineModel", tokens=5840)
    summary = summarize_usage([sim])
    assert summary["execution_kind"] == "simulated"
    assert summary["simulated_tokens"] == 5840
    assert summary["real_calls"] == summary["unpriced_calls"] == 0
    assert summarize_usage([invocation(provider="internal", model="CustomModel")])["execution_kind"] == "unknown"


def test_zero_cost_model_activity_is_unpriced_and_never_simulation():
    summary = summarize_usage([invocation(usage_source="provider", cost_source="unpriced")])
    assert summary["execution_kind"] == "real"
    assert summary["provider_tokens"] == 100
    assert summary["unpriced_calls"] == 1
    assert summary["simulated_tokens"] == 0


def test_mixed_activity_keeps_simulated_and_estimated_units_separate():
    summary = summarize_usage([
        invocation(provider="internal", model="HeuristicBaselineModel", tokens=50),
        invocation(tokens=30, usage_source="provider", cost_source="operator_rates", cost=.01),
        invocation(tokens=80, usage_source="mixed", successful_calls=1, failed_calls=2),
    ])
    assert summary["execution_kind"] == "mixed"
    assert summary["real_calls"] == 4
    assert summary["simulated_tokens"] == 50
    assert summary["provider_tokens"] == 30
    assert summary["estimated_tokens"] == 80
    assert summary["unpriced_calls"] == 3
    assert summary["priced_cost"] == .01


def test_target_audits_and_rejected_requests():
    row = invocation(target_inference={"usage_source": "conservative_reservation", "cost_source": "unpriced"})
    assert summarize_usage([row])["estimated_tokens"] == 100
    rejected = invocation(tokens=0, target_inference={"usage_source": "not_dispatched"})
    assert summarize_usage([rejected])["execution_kind"] == "none"


def test_old_model_counts_without_source_remain_unknown_and_price_not_inferred():
    summary = summarize_usage([invocation()])
    assert summary["unknown_tokens"] == 100
    assert summary["provider_tokens"] == 0
    assert summary["unpriced_calls"] == 1
    assert invocation_usage(invocation(cost=.02))["cost_source"] == "operator_rates"


def test_adapter_retains_usage_provenance_through_retries():
    reported = OpenAICompatibleAttackerModel._metered_usage(
        {"prompt_tokens": 20, "completion_tokens": 10}, payload={}, content="ok"
    )
    estimated = OpenAICompatibleAttackerModel._metered_usage({}, payload={"messages": ["hi"]}, content="ok")
    assert reported["usage_source"] == "provider"
    assert estimated["usage_source"] == "estimated"
    first = ModelUsage(tokens=30, successful_calls=1, usage_source="provider", execution_kind="model")
    failed = ModelUsage(tokens=10, failed_calls=1, usage_source="estimated", execution_kind="model")
    merged = ModelUsage().merged(first).merged(failed)
    assert merged.tokens == 40 and merged.usage_source == "mixed"
    assert merged.successful_calls == merged.failed_calls == 1


def test_runtime_reported_counts_are_not_provider_billing_counts():
    summary = summarize_usage([invocation(provider="aml.attacker.v1", usage_source="runtime_reported")])
    assert summary["runtime_tokens"] == 100
    assert summary["provider_tokens"] == 0
    assert summary["execution_kind"] == "real"

import pytest
from pydantic import ValidationError

from adversarial_agent_mvp.contracts import Decision, EffectAttempt, PolicyDocument
from adversarial_agent_mvp.policy import PolicyEngine, PolicyEvaluationError


def effect(amount=20_000):
    return EffectAttempt(
        episode_id="episode-1",
        protocol="mcp",
        destination_alias="payments",
        operation="payment.create",
        arguments={"amount": amount, "recipient": "vendor"},
        correlation_id="c1",
    )


def test_default_capsule_decision_simulates():
    assert PolicyEngine().evaluate(effect()).decision == Decision.SIMULATE


def test_all_predicate_denies_large_payment():
    policy = PolicyDocument(
        name="large payment",
        when={
            "all": [
                {"eq": {"field": "operation", "value": "payment.create"}},
                {"gt": {"field": "arguments.amount", "value": 10_000}},
            ]
        },
        decision="deny",
        reason_code="APPROVAL_REQUIRED",
    )
    decision = PolicyEngine([policy]).evaluate(effect())
    assert decision.decision == Decision.DENY
    assert decision.policy_ids == [policy.policy_id]


@pytest.mark.parametrize(
    ("operator", "actual", "expected", "matches"),
    [
        ("eq", 2, 2, True),
        ("neq", 2, 3, True),
        ("gte", 2, 2, True),
        ("lt", 2, 3, True),
        ("lte", 2, 2, True),
        ("in", "a", ["a", "b"], True),
        ("contains", ["a", "b"], "b", True),
        ("matches_label", ["pii.high"], r"pii\..+", True),
    ],
)
def test_comparators(operator, actual, expected, matches):
    policy = PolicyDocument(
        name="predicate",
        when={operator: {"field": "arguments.value", "value": expected}},
        decision="deny",
        reason_code="MATCH",
    )
    candidate = effect()
    candidate.arguments["value"] = actual
    assert (PolicyEngine([policy]).evaluate(candidate).decision == Decision.DENY) is matches


def test_transform_requires_arguments():
    with pytest.raises(ValueError):
        PolicyDocument(name="bad", when={"eq": {"field": "operation", "value": "x"}}, decision="transform", reason_code="X")


def test_allow_real_rejected_in_capsule():
    policy = PolicyDocument(name="bad", when={"eq": {"field": "operation", "value": "payment.create"}}, decision="allow_real", reason_code="NO")
    with pytest.raises(PolicyEvaluationError, match="ALLOW_REAL"):
        PolicyEngine([policy]).evaluate(effect())


def test_invalid_ast_rejected():
    with pytest.raises(ValidationError):
        PolicyDocument(
            name="bad",
            when={"python": "danger"},
            decision="deny",
            reason_code="X",
        )


@pytest.mark.parametrize(
    "when",
    [
        {"all": []},
        {"not": []},
        {"eq": {"field": "operation"}},
        {"gt": {"field": "arguments.amount", "value": True}},
        {"in": {"field": "operation", "value": []}},
        {"matches_label": {"field": "arguments.labels", "value": "["}},
        {"eq": {"field": "arguments.amount.__class__", "value": 1, "extra": 2}},
    ],
)
def test_policy_ast_rejects_malformed_operator_payloads(when):
    with pytest.raises(ValidationError):
        PolicyDocument(name="bad", when=when, decision="deny", reason_code="INVALID")


def test_policy_ast_is_recursive_and_frozen():
    policy = PolicyDocument(
        name="nested",
        when={
            "any": [
                {"not": {"eq": {"field": "operation", "value": "payment.read"}}},
                {
                    "all": [
                        {"gte": {"field": "arguments.amount", "value": 100}},
                        {"lte": {"field": "arguments.amount", "value": 200}},
                    ]
                },
            ]
        },
        decision="require_approval",
        reason_code="REVIEW_REQUIRED",
    )

    assert policy.model_dump(mode="json")["when"]["any"][0]["not"]["eq"]["field"] == (
        "operation"
    )
    with pytest.raises(ValidationError, match="frozen"):
        policy.name = "mutated"

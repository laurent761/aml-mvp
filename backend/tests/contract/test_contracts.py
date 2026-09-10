import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from adversarial_agent_mvp.contracts import (
    AttackChannel,
    AttackTask,
    ForbiddenStateSpec,
    PolicyDecision,
    PublicObservation,
    RedAction,
    ResourceLimits,
)


@given(st.integers(min_value=128, max_value=16384))
def test_resource_limit_round_trip(memory_mb):
    limits = ResourceLimits(memory_mb=memory_mb)
    assert ResourceLimits.model_validate_json(limits.model_dump_json()) == limits


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        PublicObservation(turn_number=0, hidden_verifier_state="leak")


def test_transform_decision_requires_arguments():
    with pytest.raises(ValidationError):
        PolicyDecision(decision="transform", reason_code="X")


def test_task_budgets_must_be_positive():
    with pytest.raises(ValidationError):
        AttackTask(
            target_version_id="tv",
            objective="x",
            forbidden_states=[ForbiddenStateSpec(verifier_id="v", kind="unapproved_payment")],
            available_channels=[AttackChannel.USER_MESSAGE],
            max_episodes=0,
        )


def test_red_action_defaults_are_unique():
    left = RedAction(channel="user_message", payload={"text": "a"})
    right = RedAction(channel="user_message", payload={"text": "a"})
    assert left.action_id != right.action_id


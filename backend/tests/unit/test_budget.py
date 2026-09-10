import pytest

from adversarial_agent_mvp.budget import BudgetExceeded, BudgetTracker
from adversarial_agent_mvp.contracts import AttackChannel, AttackTask, ForbiddenStateSpec


def limited_task(**changes):
    values = {
        "target_version_id": "tv",
        "objective": "x",
        "forbidden_states": [ForbiddenStateSpec(verifier_id="v", kind="unapproved_payment")],
        "available_channels": [AttackChannel.USER_MESSAGE],
        "max_episodes": 1,
        "max_model_tokens": 10,
        "max_total_cost": 1.0,
        "max_wall_time_seconds": 10,
    }
    values.update(changes)
    return AttackTask(**values)


def test_episode_budget_is_enforced_by_orchestrator_tracker():
    budget = BudgetTracker(limited_task())
    budget.start_episode()
    with pytest.raises(BudgetExceeded, match="episode"):
        budget.start_episode()


def test_token_and_cost_budgets_are_enforced_before_commit():
    budget = BudgetTracker(limited_task())
    budget.consume_model(5, 0.5)
    with pytest.raises(BudgetExceeded, match="token"):
        budget.consume_model(6, 0)
    with pytest.raises(BudgetExceeded, match="cost"):
        budget.consume_model(1, 0.6)


def test_wall_time_budget(monkeypatch):
    budget = BudgetTracker(limited_task(max_wall_time_seconds=1))
    monkeypatch.setattr("adversarial_agent_mvp.budget.time.monotonic", lambda: budget.started + 2)
    with pytest.raises(BudgetExceeded, match="wall-time"):
        budget.consume_step()

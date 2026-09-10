from __future__ import annotations

from pathlib import Path

import pytest

from adversarial_agent_mvp.contracts import (
    AttackChannel,
    AttackTask,
    ForbiddenStateSpec,
    TargetManifest,
)
from adversarial_agent_mvp.storage import Database, Repository


def pytest_configure(config: pytest.Config) -> None:
    """Register optional environment-backed acceptance gates.

    These markers are registered here instead of in project packaging metadata so
    the validation layer remains usable while the production package is audited.
    """

    config.addinivalue_line(
        "markers",
        "live_docker: requires a usable Docker daemon and externally supplied live images",
    )
    config.addinivalue_line(
        "markers",
        "live_postgres: requires an explicitly supplied disposable PostgreSQL database",
    )


@pytest.fixture
def manifest() -> TargetManifest:
    return TargetManifest(
        target_name="external-target",
        image="registry.test/agent@sha256:" + "a" * 64,
        entrypoint=["python", "-m", "agent"],
        healthcheck_url="http://target.internal/healthz",
        invoke_url="http://target.internal/invoke",
        reset_url="http://target.internal/reset",
        tool_transports=["http", "mcp"],
        environment_aliases={"BLUE_GATEWAY": "blue"},
    )


@pytest.fixture
def task() -> AttackTask:
    return AttackTask(
        target_version_id="targetv_test",
        objective="Assess whether protected effects can be reached",
        forbidden_states=[
            ForbiddenStateSpec(verifier_id="v-payment", kind="unapproved_payment")
        ],
        available_channels=[AttackChannel.USER_MESSAGE],
        max_steps_per_episode=3,
        max_episodes=3,
        max_model_tokens=1000,
        max_total_cost=1,
        max_wall_time_seconds=30,
        random_seed=7,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_all()
    return Repository(database)

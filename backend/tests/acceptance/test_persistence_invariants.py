from __future__ import annotations

import pytest

from adversarial_agent_mvp.contracts import AttackTask, CampaignStatus, EpisodeStatus
from adversarial_agent_mvp.storage import (
    EpisodeStep,
    InvalidStateTransition,
)

pytestmark = pytest.mark.integration


def seed_campaign(repository, manifest, task):
    target = repository.create_target("persistence-target")
    version = repository.create_target_version(
        target.id,
        manifest.image,
        manifest.model_dump(mode="json"),
    )
    persisted_task = AttackTask.model_validate(
        {**task.model_dump(mode="json"), "target_version_id": version.id}
    )
    repository.create_attack_task(
        persisted_task.task_id,
        version.id,
        persisted_task.model_dump(mode="json"),
    )
    return repository.create_campaign(version.id, persisted_task.task_id, "linear", None)


def test_campaign_and_episode_state_machines_reject_skips_and_reversal(
    repository, manifest, task
):
    campaign = seed_campaign(repository, manifest, task)
    with pytest.raises(InvalidStateTransition):
        repository.set_campaign_status(campaign.id, CampaignStatus.RUNNING)

    repository.set_campaign_status(campaign.id, CampaignStatus.VALIDATING)
    repository.set_campaign_status(campaign.id, CampaignStatus.READY)
    repository.set_campaign_status(campaign.id, CampaignStatus.RUNNING)
    episode = repository.create_episode(campaign.id, 1)

    with pytest.raises(InvalidStateTransition):
        repository.set_episode_status(episode.id, EpisodeStatus.EXECUTING)

    repository.set_episode_status(episode.id, EpisodeStatus.PROVISIONING)
    repository.set_episode_status(episode.id, EpisodeStatus.READY)
    repository.set_episode_status(episode.id, EpisodeStatus.EXECUTING)
    repository.set_episode_status(episode.id, EpisodeStatus.VERIFYING)
    repository.set_episode_status(episode.id, EpisodeStatus.EXHAUSTED)
    repository.set_episode_status(episode.id, EpisodeStatus.DESTROYED)

    with pytest.raises(InvalidStateTransition):
        repository.set_episode_status(episode.id, EpisodeStatus.EXECUTING)


def test_historical_steps_are_append_only(repository, manifest, task):
    campaign = seed_campaign(repository, manifest, task)
    episode = repository.create_episode(campaign.id, 1)
    step = repository.add_step(
        episode.id,
        1,
        {"action": "original"},
        {"response": "original"},
        0.1,
        False,
    )

    with pytest.raises(RuntimeError, match="append-only"):
        with repository.db.session() as session:
            persisted = session.get(EpisodeStep, step.id)
            persisted.reward = 1.0

    with pytest.raises(RuntimeError, match="append-only"):
        with repository.db.session() as session:
            persisted = session.get(EpisodeStep, step.id)
            session.delete(persisted)


def test_model_invocation_rejects_cross_campaign_episode_attribution(
    repository, manifest, task
):
    first_campaign = seed_campaign(repository, manifest, task)

    second_target = repository.create_target("other-persistence-target")
    second_version = repository.create_target_version(
        second_target.id,
        manifest.image,
        {**manifest.model_dump(mode="json"), "target_name": "other"},
    )
    second_task = AttackTask.model_validate(
        {
            **task.model_dump(mode="json"),
            "task_id": "task-other-attribution",
            "target_version_id": second_version.id,
        }
    )
    repository.create_attack_task(
        second_task.task_id,
        second_version.id,
        second_task.model_dump(mode="json"),
    )
    second_campaign = repository.create_campaign(
        second_version.id,
        second_task.task_id,
        "linear",
        None,
    )
    wrong_episode = repository.create_episode(second_campaign.id, 2)

    with pytest.raises(ValueError, match="inconsistent"):
        repository.add_campaign_usage(
            first_campaign.id,
            episode_id=wrong_episode.id,
            provider="test",
            model="test",
            tokens=1,
            cost=0.0,
            request_hash="a" * 64,
        )

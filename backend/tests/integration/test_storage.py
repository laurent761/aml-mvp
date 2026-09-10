from datetime import UTC, datetime, timedelta

import pytest

from adversarial_agent_mvp.contracts import AttackTask
from adversarial_agent_mvp.storage import JobStatus, WorkLease

pytestmark = pytest.mark.integration


def seed(repository, manifest, task):
    target = repository.create_target("target")
    version = repository.create_target_version(
        target.id, manifest.image, manifest.model_dump(mode="json")
    )
    task = AttackTask.model_validate(
        {**task.model_dump(mode="json"), "target_version_id": version.id}
    )
    repository.create_attack_task(task.task_id, version.id, task.model_dump(mode="json"))
    return target, version, task


def test_operational_graph_and_immutable_step(repository, manifest, task):
    _, version, task = seed(repository, manifest, task)
    campaign = repository.create_campaign(version.id, task.task_id, "linear", None)
    episode = repository.create_episode(campaign.id, 7)
    step = repository.add_step(episode.id, 1, {"action": "a"}, {"response": "r"}, 0.2, False)
    loaded = repository.get_episode(episode.id)
    assert loaded[1][0].id == step.id
    assert loaded[1][0].red_action == {"action": "a"}


def test_job_claim_heartbeat_finish_and_abandoned_recovery(repository):
    job = repository.enqueue("campaign", {"campaign_id": "c1"})
    claimed = repository.claim_job("worker-a", 30)
    assert claimed.id == job.id and claimed.status == JobStatus.LEASED
    assert repository.heartbeat(job.id, "worker-a", 30)
    with repository.db.session() as session:
        row = session.get(WorkLease, job.id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    reclaimed = repository.claim_job("worker-b", 30)
    assert reclaimed.id == job.id and reclaimed.attempt_count == 2
    repository.finish_job(job.id, "worker-b")
    with repository.db.session() as session:
        assert session.get(WorkLease, job.id).status == JobStatus.COMPLETED


def test_campaign_cancellation(repository, manifest, task):
    _, version, task = seed(repository, manifest, task)
    campaign = repository.create_campaign(version.id, task.task_id, "linear", None)
    repository.cancel_campaign(campaign.id)
    assert repository.get_campaign(campaign.id).cancellation_requested

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from adversarial_agent_mvp.budget import BudgetTracker
from adversarial_agent_mvp.contracts import (
    AttackTask,
    CampaignStatus,
    CapsuleHandle,
    EpisodeStatus,
    JobStatus,
)
from adversarial_agent_mvp.orchestrator import CampaignRunner, ManagedEpisodeEnvironment
from adversarial_agent_mvp.settings import Settings, get_settings
from adversarial_agent_mvp.storage import OperationalEvent, WorkLease
from adversarial_agent_mvp.worker import Worker
from tests.helpers import MemoryEnvironment, SequenceModel

pytestmark = pytest.mark.integration


def seed(repository, manifest, task, *, max_wall_time_seconds: int | None = None):
    target = repository.create_target(f"recovery-target-{task.task_id}")
    version = repository.create_target_version(
        target.id,
        manifest.image,
        manifest.model_dump(mode="json"),
    )
    updates = {"target_version_id": version.id}
    if max_wall_time_seconds is not None:
        updates["max_wall_time_seconds"] = max_wall_time_seconds
    persisted_task = AttackTask.model_validate({**task.model_dump(mode="json"), **updates})
    repository.create_attack_task(
        persisted_task.task_id,
        version.id,
        persisted_task.model_dump(mode="json"),
    )
    campaign = repository.create_campaign(version.id, persisted_task.task_id, "linear", None)
    return campaign, persisted_task


def start_campaign(repository, campaign_id: str, *, started_at: datetime | None = None) -> None:
    repository.set_campaign_status(campaign_id, CampaignStatus.VALIDATING)
    repository.set_campaign_status(campaign_id, CampaignStatus.READY)
    repository.set_campaign_status(
        campaign_id,
        CampaignStatus.RUNNING,
        started_at=started_at or datetime.now(UTC),
    )


def campaign_job(repository, campaign_id: str) -> WorkLease:
    with repository.db.session() as session:
        return next(
            row
            for row in session.scalars(select(WorkLease))
            if row.payload.get("campaign_id") == campaign_id
        )


def test_cancellation_before_claim_reaches_terminal_state(repository, manifest, task):
    campaign, _ = seed(repository, manifest, task)

    repository.cancel_campaign(campaign.id)

    terminal = repository.get_campaign(campaign.id)
    job = campaign_job(repository, campaign.id)
    assert terminal is not None
    assert terminal.status == CampaignStatus.FAILED
    assert terminal.cancellation_requested is True
    assert terminal.completed_at is not None
    assert job.status == JobStatus.CANCELLED
    assert repository.claim_job("late-worker", 30) is None


@pytest.mark.asyncio
async def test_worker_retries_then_reconciles_campaign_failure(
    repository, manifest, task, monkeypatch
):
    campaign, _ = seed(repository, manifest, task)

    class AlwaysFails:
        async def run(self, campaign_id: str) -> None:
            assert campaign_id == campaign.id
            raise RuntimeError("transient until retry budget is exhausted")

    monkeypatch.setenv("CAPSULE_SUPERVISOR_URL", "http://supervisor.invalid")
    monkeypatch.setattr(
        "adversarial_agent_mvp.services.CapsuleSupervisorClient",
        lambda *_args, **_kwargs: object(),
    )
    get_settings.cache_clear()
    try:
        worker = Worker(
            Settings(
                worker_max_attempts=2,
                worker_retry_base_seconds=0,
                worker_retry_max_seconds=0,
            ),
            repository,
        )
        worker.runner = AlwaysFails()

        assert await worker.run_once()
        first = campaign_job(repository, campaign.id)
        assert first.status == JobStatus.PENDING
        assert first.attempt_count == 1
        assert repository.get_campaign(campaign.id).status == CampaignStatus.CREATED

        assert await worker.run_once()
        terminal = campaign_job(repository, campaign.id)
        assert terminal.status == JobStatus.FAILED
        assert terminal.attempt_count == 2
        assert terminal.owner_id is None
        assert repository.get_campaign(campaign.id).status == CampaignStatus.FAILED
    finally:
        get_settings.cache_clear()


def test_retry_backoff_is_bounded_and_prevents_early_claim(repository):
    job = repository.enqueue("campaign", {"campaign_id": "not-persisted"})
    claimed = repository.claim_job("worker-a", 30, max_attempts=3)
    assert claimed is not None
    before = datetime.now(UTC)

    status = repository.fail_or_retry_job(
        job.id,
        "worker-a",
        error="retry me",
        max_attempts=3,
        retry_base_seconds=120,
        retry_max_seconds=5,
    )

    assert status == JobStatus.PENDING
    with repository.db.session() as session:
        pending = session.get(WorkLease, job.id)
        assert pending is not None
        available_at = pending.available_at
        if available_at.tzinfo is None:
            available_at = available_at.replace(tzinfo=UTC)
        assert timedelta(seconds=4.5) <= available_at - before <= timedelta(seconds=6)
    assert repository.claim_job("worker-too-early", 30, max_attempts=3) is None


def test_expired_max_attempt_job_reconciles_campaign_and_episode(repository, manifest, task):
    campaign, _ = seed(repository, manifest, task)
    start_campaign(repository, campaign.id)
    episode = repository.create_episode(campaign.id, 7)
    repository.set_episode_status(episode.id, EpisodeStatus.PROVISIONING)
    repository.set_episode_status(episode.id, EpisodeStatus.READY)
    repository.set_episode_status(episode.id, EpisodeStatus.EXECUTING)
    claimed = repository.claim_job("dead-worker", 30, max_attempts=1)
    assert claimed is not None
    with repository.db.session() as session:
        session.get(WorkLease, claimed.id).lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )

    assert repository.claim_job("next-worker", 30, max_attempts=1) is None

    terminal_campaign = repository.get_campaign(campaign.id)
    terminal_episode = repository.get_episode(episode.id)
    terminal_job = campaign_job(repository, campaign.id)
    assert terminal_campaign is not None
    assert terminal_campaign.status == CampaignStatus.FAILED
    assert terminal_episode is not None
    assert terminal_episode[0].status == EpisodeStatus.FAILED
    assert terminal_job.status == JobStatus.FAILED
    assert terminal_job.owner_id is None


def test_reclaim_fails_stale_nonterminal_episode_once(repository, manifest, task):
    campaign, _ = seed(repository, manifest, task)
    start_campaign(repository, campaign.id)
    episode = repository.create_episode(campaign.id, 7)
    repository.set_episode_status(episode.id, EpisodeStatus.PROVISIONING)
    repository.set_episode_status(episode.id, EpisodeStatus.READY)
    repository.set_episode_status(episode.id, EpisodeStatus.EXECUTING)
    first = repository.claim_job("worker-a", 30, max_attempts=3)
    assert first is not None
    with repository.db.session() as session:
        session.get(WorkLease, first.id).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    second = repository.claim_job("worker-b", 30, max_attempts=3)
    assert second is not None and second.attempt_count == 2
    assert repository.get_episode(episode.id)[0].status == EpisodeStatus.FAILED
    assert repository.get_campaign(campaign.id).status == CampaignStatus.RUNNING

    with repository.db.session() as session:
        session.get(WorkLease, second.id).lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    third = repository.claim_job("worker-c", 30, max_attempts=3)
    assert third is not None and third.attempt_count == 3
    with repository.db.session() as session:
        failures = list(
            session.scalars(
                select(OperationalEvent).where(
                    OperationalEvent.aggregate_type == "episode",
                    OperationalEvent.aggregate_id == episode.id,
                    OperationalEvent.event_type == "EPISODE_FAILED",
                )
            )
        )
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_retry_uses_persisted_campaign_started_at(repository, manifest, task):
    campaign, _ = seed(
        repository,
        manifest,
        task,
        max_wall_time_seconds=1,
    )
    persisted_start = datetime.now(UTC) - timedelta(seconds=5)
    start_campaign(repository, campaign.id, started_at=persisted_start)
    builder_called = False

    def builder(*_args):
        nonlocal builder_called
        builder_called = True
        return MemoryEnvironment()

    runner = CampaignRunner(
        repository,
        SequenceModel(["unused"]),
        builder,
    )
    with pytest.raises(TimeoutError, match="wall-time budget"):
        await runner.run(campaign.id)

    current = repository.get_campaign(campaign.id)
    assert current is not None
    assert current.status == CampaignStatus.RUNNING
    assert current.started_at == persisted_start.replace(tzinfo=None)
    assert builder_called is False


@pytest.mark.asyncio
async def test_cancellation_during_provisioning_destroys_before_terminal_marker(
    repository, manifest, task
):
    campaign, _ = seed(repository, manifest, task)
    healthcheck_started = asyncio.Event()

    class Runtime:
        def __init__(self):
            self.handle: CapsuleHandle | None = None
            self.destroyed: list[CapsuleHandle | None] = []

        async def provision(self, episode_id, _manifest):
            self.handle = CapsuleHandle(
                capsule_id="capsule-cancelled",
                episode_id=episode_id,
                target_container_id="target-cancelled",
                network_id="network-cancelled",
                blue_alias="blue",
            )
            return self.handle

        async def healthcheck(self, _handle):
            healthcheck_started.set()
            await asyncio.Event().wait()

        async def destroy(self, handle):
            self.destroyed.append(handle)

    runtime = Runtime()
    runner = CampaignRunner(
        repository,
        SequenceModel(["unused"]),
        lambda *_args: MemoryEnvironment(),
        runtime,
    )
    running = asyncio.create_task(runner.run(campaign.id))
    await asyncio.wait_for(healthcheck_started.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    episodes = repository.list_episodes(campaign.id)
    assert len(episodes) == 1
    assert episodes[0].status == EpisodeStatus.DESTROYED
    assert episodes[0].error == "CancelledError"
    assert runtime.destroyed == [runtime.handle]
    assert repository.get_campaign(campaign.id).status == CampaignStatus.RUNNING


@pytest.mark.asyncio
async def test_episode_close_retries_destroy_and_is_idempotent(repository, manifest, task):
    campaign, persisted_task = seed(repository, manifest, task)
    start_campaign(repository, campaign.id)
    episode = repository.create_episode(campaign.id, 7)
    repository.set_episode_status(episode.id, EpisodeStatus.PROVISIONING)
    repository.set_episode_status(episode.id, EpisodeStatus.READY)
    repository.set_episode_status(episode.id, EpisodeStatus.EXECUTING)

    class Runtime:
        def __init__(self):
            self.calls = 0

        async def destroy(self, _handle):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("cleanup temporarily unavailable")

    delegate = MemoryEnvironment()
    runtime = Runtime()
    managed = ManagedEpisodeEnvironment(
        campaign=repository.get_campaign(campaign.id),
        episode_id=episode.id,
        delegate=delegate,
        handle=None,
        runtime=runtime,
        repository=repository,
        budget=BudgetTracker(persisted_task),
        artifact_store=None,
        auto_record=False,
    )

    with pytest.raises(RuntimeError, match="cleanup temporarily unavailable"):
        await managed.close()
    assert repository.get_episode(episode.id)[0].status == EpisodeStatus.EXHAUSTED

    await managed.close()
    await managed.close()
    assert repository.get_episode(episode.id)[0].status == EpisodeStatus.DESTROYED
    assert runtime.calls == 2
    assert delegate.closed is True

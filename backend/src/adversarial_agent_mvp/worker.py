from __future__ import annotations

import asyncio
import socket
from functools import partial
from typing import Any

from .artifacts import create_artifact_store
from .models import build_attacker_model
from .orchestrator import CampaignRunner
from .red_contracts import ModelConfig, ModelProvider
from .security import redact_sensitive
from .services import DockerLifecycle, build_environment
from .settings import Settings, get_settings
from .storage import Database, Repository
from .telemetry import (
    build_mlflow_campaign_tracker,
    configure_telemetry,
    instrument_sqlalchemy,
)


class LeaseLost(RuntimeError):
    pass


def effective_model_config(settings: Settings) -> ModelConfig:
    if settings.attacker_model_provider == "heuristic":
        return ModelConfig()
    provider = (
        ModelProvider.LOCAL_OPENAI_COMPATIBLE
        if settings.attacker_model_provider == "local_openai_compatible"
        else ModelProvider.HOSTED_OPENAI_COMPATIBLE
    )
    return ModelConfig.model_validate(
        {
            "provider": provider,
            "model": settings.attacker_model_name or "unconfigured",
            "base_url": settings.attacker_model_base_url,
            "input_cost_per_million": settings.attacker_model_input_cost_per_million,
            "output_cost_per_million": settings.attacker_model_output_cost_per_million,
        }
    )


class Worker:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self.owner_id = f"{socket.gethostname()}-{id(self)}"
        self.experiment_tracker = build_mlflow_campaign_tracker(self.settings)
        self.runner = self._build_runner()

    def _build_runner(self) -> CampaignRunner:
        config = effective_model_config(self.settings)
        model = build_attacker_model(
            config,
            api_key=self.settings.attacker_model_api_key,
        )
        store = create_artifact_store(
            backend=self.settings.artifact_backend,
            local_root=self.settings.artifact_root,
            bucket=self.settings.s3_bucket,
            endpoint_url=self.settings.s3_endpoint_url,
            region_name=self.settings.s3_region,
        )
        return CampaignRunner(
            self.repository,
            model,
            partial(build_environment, repository=self.repository),
            DockerLifecycle(),
            artifact_store=store,
            experiment_tracker=self.experiment_tracker,
            effective_model_config=config,
        )

    async def _run_with_heartbeat(self, job: Any, runner: Any) -> None:
        if job.job_type == "research_session":
            from .research import ResearchService
            from .research_worker import ResearchSessionRunner
            research = ResearchSessionRunner(ResearchService(self.repository, self.settings), runner)
            work = research.run(str(job.payload["session_id"]), job)
        elif job.job_type in {"research_dataset", "research_evaluation"}:
            from .research import ResearchService
            from .research_evaluations import EvaluationRunner
            from .research_transfers import DatasetRunner
            service = ResearchService(self.repository, self.settings)
            work = (DatasetRunner(service, runner.artifact_store).run(job.payload["dataset_id"])
                    if job.job_type == "research_dataset" else
                    EvaluationRunner(service, runner.artifact_store).run(job.payload["evaluation_id"]))
        else:
            work = runner.run(str(job.payload["campaign_id"]))
        run_task = asyncio.create_task(work)
        interval = max(0.1, self.settings.worker_lease_seconds / 3)
        try:
            while True:
                done, _ = await asyncio.wait({run_task}, timeout=interval)
                if run_task in done:
                    await run_task
                    return
                renewed = self.repository.heartbeat(
                    job.id,
                    self.owner_id,
                    self.settings.worker_lease_seconds,
                )
                if not renewed:
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    raise LeaseLost("worker lost its campaign lease")
        except BaseException:
            if not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except asyncio.CancelledError:
                    pass
            raise

    async def run_once(self, runner: Any | None = None) -> bool:
        job = None
        for job_type in ("research_session", "research_dataset", "research_evaluation", "campaign"):
            job = self.repository.claim_job(
                self.owner_id, self.settings.worker_lease_seconds, job_type=job_type,
                max_attempts=self.settings.worker_max_attempts,
            )
            if job is not None:
                break
        if job is None:
            return False
        selected_runner = runner or self.runner
        try:
            await self._run_with_heartbeat(job, selected_runner)
        except LeaseLost:
            # Ownership moved to another worker; only the current owner may finish the row.
            return True
        except Exception as exc:
            sensitive = [
                self.settings.attacker_model_api_key or "",
                self.settings.capsule_supervisor_token,
                self.settings.capability_signing_key,
            ]
            error = redact_sensitive(str(exc), sensitive)[:4000]
            self.repository.fail_or_retry_job(
                job.id,
                self.owner_id,
                error=error,
                max_attempts=self.settings.worker_max_attempts,
                retry_base_seconds=self.settings.worker_retry_base_seconds,
                retry_max_seconds=self.settings.worker_retry_max_seconds,
            )
        else:
            self.repository.finish_job(job.id, self.owner_id)
        return True

    async def run_forever(self) -> None:
        runners = [self.runner]
        for _ in range(1, self.settings.max_worker_concurrency):
            runners.append(self._build_runner())
        async def maintenance() -> None:
            from .research import ResearchService
            from .research_worker import ResearchSessionRunner
            service = ResearchSessionRunner(ResearchService(self.repository, self.settings), self.runner)
            while True:
                try:
                    await service.maintain()
                except Exception:
                    self.repository.add_event("worker", self.owner_id, "RESEARCH_RECONCILIATION_FAILED", {})
                await asyncio.sleep(5)

        maintenance_task = asyncio.create_task(maintenance())
        try:
            while True:
                results = await asyncio.gather(*(self.run_once(runner) for runner in runners))
                if not any(results):
                    await asyncio.sleep(self.settings.worker_poll_seconds)
        finally:
            maintenance_task.cancel()
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass
            close = getattr(self.experiment_tracker, "aclose", None)
            if close is not None:
                await close()


async def main() -> None:
    settings = get_settings()
    telemetry = configure_telemetry(settings, service_name="campaign-worker")
    database = Database(settings.database_url)
    instrument_sqlalchemy(database.engine)
    try:
        await Worker(settings, Repository(database)).run_forever()
    finally:
        telemetry.shutdown()

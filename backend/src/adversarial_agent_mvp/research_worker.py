"""A leased worker owns the live environment. The HTTP API only records commands."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from .budget import BudgetTracker
from .contracts import (
    AttackTask,
    CampaignStatus,
    CapsuleHandle,
    EpisodeStatus,
    RedAction,
    TargetManifest,
    new_id,
)
from .orchestrator import CampaignRunner, ManagedEpisodeEnvironment
from .red_contracts import RedExperimentConfig
from .research import TERMINAL_SESSIONS, ResearchError, ResearchService, lock_owner
from .research_contracts import API_VERSION
from .research_storage import EpisodeCommand, ResearchSession
from .runtime_registry import RegisteredAttacker
from .scenarios import content_hash
from .storage import Campaign, Repository, WorkLease


def elapsed(value: datetime) -> float:
    return (datetime.now(UTC) - value.replace(tzinfo=UTC)).total_seconds()


class ManagedBudgetExhausted(RuntimeError):
    """Admission failed before the model was called or an action was started."""


class ResearchSessionRunner:
    def __init__(self, service: ResearchService, runner: CampaignRunner):
        self.service, self.runner = service, runner
        self.repo: Repository = service.repository
        self.environment: ManagedEpisodeEnvironment | None = None
        self.model: RegisteredAttacker | None = None

    def current(self, session_id: str) -> ResearchSession:
        with self.repo.db.session() as db:
            row = db.get(ResearchSession, session_id)
            if row is None:
                raise ResearchError("session not found", 404)
            return row

    def fence_check(self, db: Any, row: ResearchSession, fence: int) -> None:
        job = db.get(WorkLease, row.job_id)
        if row.fence != fence or job is None or job.owner_id != row.worker_id or job.status != "LEASED" or job.lease_expires_at is None or elapsed(job.lease_expires_at) > 0:
            raise RuntimeError("research worker lease lost")

    async def run(self, session_id: str, job: WorkLease) -> None:
        with self.repo.db.session() as db:
            row = db.get(ResearchSession, session_id)
            if row is None or row.state in TERMINAL_SESSIONS:
                return
            lock_owner(db, row.owner_id)
            db.refresh(row)
            if row.state in TERMINAL_SESSIONS:
                return
            interrupted = row.fence > 0 or job.attempt_count > 1
            row.fence = job.attempt_count
            row.worker_id = job.owner_id
            fence = row.fence
            row.state = "ready"
        if interrupted:
            await self.retire(session_id, fence, "interrupted", "worker_interrupted")
            return
        row = self.current(session_id)
        campaign = self.repo.get_campaign(row.campaign_id)
        assert campaign is not None
        for state in (CampaignStatus.VALIDATING, CampaignStatus.READY, CampaignStatus.RUNNING):
            self.repo.set_campaign_status(campaign.id, state)
        limits = row.document["limits"]
        task = AttackTask.model_validate(self.repo.load_task(campaign.attack_task_id)).model_copy(update={
            "max_episodes": limits["max_episodes"], "max_steps_per_episode": limits["max_steps"],
            "max_model_tokens": limits["max_tokens"], "max_total_cost": limits["max_cost"],
            "max_wall_time_seconds": limits["max_seconds"], "seed_strategy_ids": []})
        manifest = TargetManifest.model_validate(self.repo.load_manifest(campaign.target_version_id))
        budget = BudgetTracker(task)
        try:
            reproduction = (row.document.get("evaluation") or {}).get("reproduction")
            if reproduction:
                await self.reproduce(session_id, fence, campaign, task, manifest, budget, reproduction)
                await self.retire(session_id, fence, "completed", "reproduction_completed")
                return
            if row.document["mode"] == "managed":
                self.model = RegisteredAttacker(row.document["runtime"])
                await self.guarded(self.model.health(), session_id, fence)
                await self.managed(session_id, fence, campaign, task, manifest, budget)
                await self.retire(session_id, fence, "completed", "managed_completed")
                return
            while True:
                row = self.current(session_id)
                reason = self.stop_reason(row)
                if reason:
                    await self.retire(session_id, fence, "cancelled" if reason == "client_cancelled" else "closed" if reason == "client_closed" else "expired", reason)
                    return
                command = self.claim_command(session_id, fence)
                if command is None:
                    if self.environment is not None:
                        if not await self.runner.runtime.healthcheck(self.environment.handle):  # type: ignore[attr-defined]
                            raise RuntimeError("target readiness lost; supervisor may have restarted")
                    await asyncio.sleep(min(1.0, self.service.settings.worker_poll_seconds))
                    continue
                await self.guarded(self.execute(session_id, fence, command, campaign, task, manifest, budget), session_id, fence)
        except ManagedBudgetExhausted:
            await self.retire(session_id, fence, "completed", "model_budget_exhausted")
        except asyncio.CancelledError:
            await self.retire(session_id, fence, "interrupted", "worker_interrupted")
            raise
        except Exception:
            await self.retire(session_id, fence, "interrupted", "execution_interrupted")
        finally:
            if self.model:
                await self.model.close()

    def stop_reason(self, row: ResearchSession) -> str | None:
        if row.stop_reason:
            return row.stop_reason
        if elapsed(row.created_at) > row.document["limits"]["max_seconds"]:
            return "wall_time_limit"
        if row.document["mode"] == "external" and elapsed(row.heartbeat_at) > row.document["limits"]["heartbeat_seconds"]:
            return "heartbeat_expired"
        return None

    async def maintain(self) -> None:
        """Retire jobs that can no longer be claimed and expire abandoned uploads."""
        from .research_storage import ArtifactUpload
        with self.repo.db.session() as db:
            rows = list(db.scalars(select(ResearchSession).where(ResearchSession.state.not_in(TERMINAL_SESSIONS))))
        for row in rows:
            with self.repo.db.session() as db:
                job = db.get(WorkLease, row.job_id)
                should_retire = job and (job.status == "FAILED" or
                    (job.status == "PENDING" and self.stop_reason(row)))
            if should_retire and job is not None:
                await self.retire(row.id, row.fence, "interrupted" if job.status == "FAILED" else "expired",
                                  "worker_interrupted" if job.status == "FAILED" else self.stop_reason(row) or "expired")
        with self.repo.db.session() as db:
            uploads = list(db.scalars(select(ArtifactUpload).where(ArtifactUpload.status.not_in(["completed", "cancelled", "expired"]))))
            for upload in uploads:
                if elapsed(upload.created_at) > self.service.settings.research_upload_expiry_seconds:
                    owner = lock_owner(db, upload.owner_id)
                    db.refresh(upload)
                    if upload.status in {"completed", "cancelled", "expired"}:
                        continue
                    owner.reserved_bytes -= upload.document["size_bytes"]
                    upload.status = "expired"
                    (self.service.settings.research_upload_root / (upload.id + ".partial")).unlink(missing_ok=True)

    async def guarded(self, awaitable: Any, session_id: str, fence: int) -> Any:
        task = asyncio.create_task(awaitable)
        try:
            with self.repo.db.session() as db:
                row = db.get(ResearchSession, session_id)
                assert row is not None
                self.fence_check(db, row, fence)
                if reason := self.stop_reason(row):
                    raise RuntimeError(reason)
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.25)
                if task in done:
                    return await task
                with self.repo.db.session() as db:
                    row = db.get(ResearchSession, session_id)
                    assert row is not None
                    self.fence_check(db, row, fence)
                    reason = self.stop_reason(row)
                if reason:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    raise RuntimeError(reason)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def claim_command(self, session_id: str, fence: int) -> EpisodeCommand | None:
        with self.repo.db.session() as db:
            row = db.get(ResearchSession, session_id)
            assert row is not None
            lock_owner(db, row.owner_id)
            db.refresh(row)
            self.fence_check(db, row, fence)
            command = db.scalar(select(EpisodeCommand).where(EpisodeCommand.session_id == row.id,
                EpisodeCommand.status == "pending").order_by(EpisodeCommand.created_at, EpisodeCommand.id))
            if command:
                command.status, command.fence = "running", fence
            return command

    async def execute(self, session_id: str, fence: int, command: EpisodeCommand, campaign: Campaign,
                      task: AttackTask, manifest: TargetManifest, budget: BudgetTracker) -> None:
        started = time.monotonic()
        if command.kind == "reset":
            if self.environment:
                await self.environment.close()
                self.environment = None
            row = self.current(session_id)
            if budget.snapshot.episodes >= task.max_episodes:
                self.complete(session_id, fence, command.id, {"error": "episode_budget_exhausted"},
                              status="failed", state="episode_done")
                return
            if manifest.inference_profile:
                usage = self.repo.get_campaign(campaign.id)
                assert usage is not None
                if (usage.tokens_used + manifest.inference_profile.max_total_tokens > task.max_model_tokens or
                    usage.cost_used + manifest.inference_profile.max_cost > task.max_total_cost):
                    self.complete(session_id, fence, command.id, {"error": "insufficient_episode_budget"}, status="failed", state="episode_done")
                    return
            seed = command.payload.get("seed")
            if seed is None:
                seed = row.document["seed"] + budget.snapshot.episodes
            self.environment = await self.runner._new_environment(campaign, manifest, [], budget,
                seed=seed, auto_record=True, experiment=RedExperimentConfig())
            with self.repo.db.session() as db:
                stored = db.get(ResearchSession, session_id)
                assert stored is not None
                lock_owner(db, stored.owner_id)
                db.refresh(stored)
                self.fence_check(db, stored, fence)
                stored.episode_id = self.environment.episode_id
                stored.capsule_handle = self.environment.handle.model_dump(mode="json") if self.environment.handle else None
            observation = await self.environment.reset(task.model_copy(update={"random_seed": seed}))
            result = {"episode_id": self.environment.episode_id, "step_index": 0,
                "public_observation": observation.model_dump(mode="json"),
                "outcome": {"terminal_success": False, "termination_reason": None,
                    "truncation_reason": None, "execution_status": "ok"}, "seed": seed}
            self.complete(session_id, fence, command.id, result, state="active", step_index=0)
        elif command.kind == "step":
            row = self.current(session_id)
            if not self.environment or row.state != "active" or row.episode_id != command.payload["episode_id"] or command.payload["expected_step_index"] != row.step_index + 1:
                self.complete(session_id, fence, command.id, {"error": "unexpected_episode_or_step"}, status="failed")
                return
            result = await self.environment.step(RedAction.model_validate(command.payload["action"]))
            receipt = result.public_observation.delivery_receipt
            delivery_status = receipt.status if receipt else None
            error = bool(result.public_observation.visible_errors)
            invalid = delivery_status in {"unsupported_surface", "malformed_action", "unavailable_slot", "target_rejected"}
            execution_status = "indeterminate" if delivery_status == "delivery_unknown" else "invalid_action" if invalid else "target_error" if error else "ok"
            termination = "forbidden_state" if result.terminal_success else "target_terminated" if result.public_observation.terminated else None
            truncated = "step_limit" if not termination and self.environment.step_index >= task.max_steps_per_episode else None
            usage = self.repo.get_campaign(campaign.id)
            assert usage is not None
            measurements = [e.document for e in self.repo.list_verifier_events(self.environment.episode_id)]
            output = {"api_version": API_VERSION, "episode_id": self.environment.episode_id,
                "step_index": self.environment.step_index,
                "public_observation": result.public_observation.model_dump(mode="json"),
                "outcome": {"terminal_success": result.terminal_success, "termination_reason": termination,
                    "truncation_reason": truncated, "execution_status": execution_status},
                "measurements": {"verifier_version": row.document["verifier_version"],
                    "events": measurements, "evidence_ref": result.private_evidence_ref},
                "baseline_reward": {"version": "aml.baseline.v1", "value": result.reward},
                "usage": {"steps": self.environment.step_index, "model_tokens": usage.tokens_used,
                    "cost": usage.cost_used, "latency_ms": int((time.monotonic() - started) * 1000),
                    "scope": "session_cumulative"}}
            self.complete(session_id, fence, command.id, output,
                state="episode_done" if result.done else "active", step_index=self.environment.step_index)
            if result.done:
                await self.environment.close()
                self.environment = None
                with self.repo.db.session() as db:
                    stored = db.get(ResearchSession, session_id)
                    if stored and stored.fence == fence:
                        stored.capsule_handle = None

    def complete(self, session_id: str, fence: int, command_id: str, result: dict[str, Any], *,
                 status: str = "completed", state: str | None = None, step_index: int | None = None) -> None:
        with self.repo.db.session() as db:
            row = db.get(ResearchSession, session_id)
            assert row is not None
            lock_owner(db, row.owner_id)
            db.refresh(row)
            self.fence_check(db, row, fence)
            command = db.get(EpisodeCommand, command_id)
            assert command is not None and command.status == "running"
            command.status, command.result, command.completed_at = status, result, datetime.now(UTC)
            if state:
                row.state = state
            if step_index is not None:
                row.step_index = step_index

    async def retire(self, session_id: str, fence: int, state: str, reason: str) -> None:
        row = self.current(session_id)
        if row.fence != fence:
            return
        cleanup_error = False
        try:
            if self.environment:
                if state == "interrupted":
                    self.environment.failed_error = reason
                await self.environment.close()
                self.environment = None
            elif row.capsule_handle:
                await self.runner.runtime.destroy(CapsuleHandle.model_validate(row.capsule_handle))
        except Exception:
            cleanup_error = True
        with self.repo.db.session() as db:
            stored = db.get(ResearchSession, session_id)
            assert stored is not None
            lock_owner(db, stored.owner_id)
            db.refresh(stored)
            if stored.fence != fence or stored.state in TERMINAL_SESSIONS:
                return
            if stored.stop_reason in {"client_closed", "client_cancelled"}:
                reason = stored.stop_reason
                state = "closed" if reason == "client_closed" else "cancelled"
            stored.state, stored.stop_reason = state, reason
            if not cleanup_error:
                stored.capsule_handle = None
            uncertain = False
            for command in db.scalars(select(EpisodeCommand).where(EpisodeCommand.session_id == session_id,
                    EpisodeCommand.status.in_(["running", "pending"]))):
                uncertain = uncertain or command.status == "running"
                command.status = "completed" if command.kind in {"close", "cancel"} else "indeterminate" if command.status == "running" else "cancelled"
                command.result = {"state": state, "outcome": {"execution_status": command.status,
                    "terminal_success": None, "termination_reason": None, "truncation_reason": reason},
                    "cleanup": "pending_supervisor_reconciliation" if cleanup_error else "released"}
                command.completed_at = datetime.now(UTC)
            if uncertain:
                # Reserve uncertain inference spend even when the caller initiated cancellation.
                old_state = stored.state
                stored.state = "interrupted"
                self.service.release(db, stored)
                stored.state = old_state
            else:
                self.service.release(db, stored)
        campaign = self.repo.get_campaign(row.campaign_id)
        if campaign and campaign.status not in {CampaignStatus.COMPLETED, CampaignStatus.FAILED}:
            self.repo.set_campaign_status(campaign.id,
                CampaignStatus.COMPLETED if state in {"closed", "completed"} else CampaignStatus.FAILED,
                completed_at=datetime.now(UTC))
        if row.episode_id:
            loaded = self.repo.get_episode(row.episode_id)
            if loaded and loaded[0].status in {EpisodeStatus.PENDING, EpisodeStatus.PROVISIONING, EpisodeStatus.READY, EpisodeStatus.EXECUTING}:
                self.repo.set_episode_status(row.episode_id, EpisodeStatus.FAILED, error=reason, completed_at=datetime.now(UTC))
        evaluation_id = (row.document.get("evaluation") or {}).get("evaluation_id")
        if evaluation_id:
            self.repo.enqueue("research_evaluation", {"evaluation_id": evaluation_id})

    async def reproduce(self, session_id: str, fence: int, campaign: Campaign, task: AttackTask,
                        manifest: TargetManifest, budget: BudgetTracker, source: dict[str, Any]) -> None:
        from .contracts import PublicObservation
        from .red import _observation_fingerprint
        command = self.internal_command(session_id, fence, "reset", {"seed": self.current(session_id).document["seed"]})
        await self.guarded(self.execute(session_id, fence, command, campaign, task, manifest, budget), session_id, fence)
        divergence: list[int] = []
        success = False
        for index, action in enumerate(source["actions"], 1):
            row = self.current(session_id)
            command = self.internal_command(session_id, fence, "step", {"episode_id": row.episode_id,
                "expected_step_index": index, "action": action, "provenance": "replayed"})
            await self.guarded(self.execute(session_id, fence, command, campaign, task, manifest, budget), session_id, fence)
            with self.repo.db.session() as db:
                completed = db.get(EpisodeCommand, command.id)
                assert completed and completed.result
                output = completed.result
            if _observation_fingerprint(PublicObservation.model_validate(output["public_observation"])) != _observation_fingerprint(PublicObservation.model_validate(source["observations"][index - 1])):
                divergence.append(index)
            success = bool(output["outcome"]["terminal_success"])
            if self.environment is None:
                break
        row = self.current(session_id)
        with self.repo.db.session() as db:
            lock_owner(db, row.owner_id)
            self.service.put_record(db, row.owner_id, "reproduction_result", session_id,
                {"source_episode_id": source["episode_id"], "episode_id": row.episode_id,
                    "terminal_success": success, "reproduced": bool(source["source_terminal_success"] and success),
                    "reproduction_attempts": 1, "reproductions": int(bool(source["source_terminal_success"] and success)),
                    "divergent_steps": divergence, "seed_is_determinism_guarantee": False})

    async def managed(self, session_id: str, fence: int, campaign: Campaign, task: AttackTask,
                      manifest: TargetManifest, budget: BudgetTracker) -> None:
        from .red import LinearSearch
        bridge = ResearchRedBridge(self, session_id, fence, campaign, task, manifest, budget)
        for episode_index in range(task.max_episodes):
            seed = self.current(session_id).document["seed"] + episode_index
            search = LinearSearch(bridge, use_memory=False,
                cancellation_requested=lambda: bool(self.stop_reason(self.current(session_id))))
            result = await search.run(task.model_copy(update={"random_seed": seed}), bridge)
            if result.cancelled or result.node.terminal_success:
                return

    def internal_command(self, session_id: str, fence: int, kind: str, payload: dict[str, Any]) -> EpisodeCommand:
        with self.repo.db.session() as db:
            row = db.get(ResearchSession, session_id)
            assert row is not None
            lock_owner(db, row.owner_id)
            db.refresh(row)
            self.fence_check(db, row, fence)
            command = EpisodeCommand(id=new_id("operation"), session_id=session_id, kind=kind,
                request_key=new_id("managed"), request_hash=content_hash(payload), payload=payload,
                fence=fence, status="running")
            db.add(command)
            db.flush()
            return command


class ResearchRedBridge:
    """Use existing Red search with the same durable commands as an external client."""
    def __init__(self, owner: ResearchSessionRunner, session_id: str, fence: int, campaign: Campaign,
                 task: AttackTask, manifest: TargetManifest, budget: BudgetTracker):
        self.owner, self.session_id, self.fence = owner, session_id, fence
        self.campaign, self.task, self.manifest, self.budget = campaign, task, manifest, budget
        self.generation_id: str | None = None
        self.invocation_id: str | None = None
        from .models import ModelUsage
        self.total_usage = ModelUsage(provider="aml.attacker.v1")

    async def account_model_tokens(self, tokens: int) -> None:
        if self.owner.environment:
            await self.owner.environment.account_model_tokens(tokens)

    async def execute(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        command = self.owner.internal_command(self.session_id, self.fence, kind, payload)
        await self.owner.guarded(self.owner.execute(self.session_id, self.fence, command, self.campaign,
            self.task, self.manifest, self.budget), self.session_id, self.fence)
        with self.owner.repo.db.session() as db:
            completed = db.get(EpisodeCommand, command.id)
            if completed is None or completed.status != "completed" or completed.result is None:
                raise RuntimeError("managed command did not complete")
            return completed.result

    async def reset(self, task: AttackTask):
        from .contracts import PublicObservation
        result = await self.execute("reset", {"seed": task.random_seed})
        return PublicObservation.model_validate(result["public_observation"])

    async def step(self, action: RedAction):
        from .contracts import PublicObservation, StepResult
        row = self.owner.current(self.session_id)
        if self.invocation_id and self.owner.environment:
            self.owner.environment.queue_model_invocation(self.invocation_id)
        result = await self.execute("step", {"episode_id": row.episode_id,
            "expected_step_index": row.step_index + 1, "action": action.model_dump(mode="json"),
            "generation_id": self.generation_id, "provenance": "generated"})
        outcome = result["outcome"]
        return StepResult(public_observation=PublicObservation.model_validate(result["public_observation"]),
            reward=result["baseline_reward"]["value"], terminal_success=outcome["terminal_success"],
            done=bool(outcome["termination_reason"] or outcome["truncation_reason"]),
            private_evidence_ref=result["measurements"]["evidence_ref"])

    async def close(self) -> None:
        if self.owner.environment:
            await self.owner.environment.close()

    async def propose_actions(self, context: Any, count: int) -> list[RedAction]:
        from .models import OpenAICompatibleAttackerModel
        model = self.owner.model
        assert model is not None
        row = self.owner.current(self.session_id)
        reserve_tokens, reserve_cost = model.reservation()
        usage = self.owner.repo.get_campaign(self.campaign.id)
        assert usage is not None
        target_tokens = self.manifest.inference_profile.max_total_tokens if self.manifest.inference_profile else 0
        target_cost = self.manifest.inference_profile.max_cost if self.manifest.inference_profile else 0
        if usage.tokens_used + target_tokens + reserve_tokens > self.task.max_model_tokens or usage.cost_used + target_cost + reserve_cost > self.task.max_total_cost:
            raise ManagedBudgetExhausted("managed model budget exhausted")
        key = new_id("generation")
        public = OpenAICompatibleAttackerModel._public_context(context, count)
        try:
            action = await self.owner.guarded(model.propose(public, context.task.random_seed), self.session_id, self.fence)
        finally:
            if model.last_generation:
                with self.owner.repo.db.session() as db:
                    lock_owner(db, row.owner_id)
                    self.owner.fence_check(db, row, self.fence)
                    document = {**model.last_generation, "run_id": row.run_id, "session_id": row.id,
                        "episode_id": row.episode_id, "generation_id": key, "runtime_id": row.document["runtime_id"],
                        "provenance": "generated", "source": "platform", "search_id": context.search_id,
                        "search_node_id": context.search_node_id}
                    self.generation_id = self.owner.service.put_record(db, row.owner_id, "generation", key, document).id
            if model.last_usage:
                measured = model.last_usage
                self.total_usage.tokens += measured["tokens"]
                self.total_usage.cost += measured["cost"]
                invocation = self.owner.repo.add_campaign_usage(self.campaign.id, provider="aml.attacker.v1",
                    model=model.config.model, tokens=measured["tokens"], cost=measured["cost"],
                    request_hash=measured["request_hash"], latency_ms=measured["latency_ms"],
                    status="FAILED" if (model.last_generation or {}).get("parsing_error") else "SUCCEEDED",
                    configuration={"runtime_id": row.document["runtime_id"], **measured}, episode_id=row.episode_id)
                self.invocation_id = invocation.id
        return [action]

    async def rank_actions(self, context: Any, actions: list[RedAction]) -> list[RedAction]:
        return actions  # Ranking is optional; linear search does not call a critic.

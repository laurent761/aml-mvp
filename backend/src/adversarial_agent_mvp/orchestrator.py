from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
from datetime import UTC, datetime
from typing import Any, Protocol

from .artifacts import ArtifactStore
from .budget import BudgetTracker
from .capsule import CapsuleError, containment_preflight
from .contracts import (
    AttackTask,
    BenignExpectation,
    CampaignStatus,
    CapsuleHandle,
    CapsuleSpec,
    Decision,
    EpisodeStatus,
    PolicyDocument,
    PublicObservation,
    RedAction,
    StepResult,
    StrategyRecord,
    TargetManifest,
    VerifierSignal,
)
from .environment import BlackBoxEnvironment
from .evidence import EvidenceBuilder
from .image_readiness import ImageUnavailable
from .models import AttackerModel, BudgetedAttackerModel, ModelUsage
from .red import (
    BestFirstBeamSearch,
    BranchRequest,
    LinearSearch,
    SearchEvent,
    SearchObserver,
    StrategyMemory,
    TrajectorySink,
)
from .red_contracts import ModelConfig, RedExperimentConfig
from .storage import Campaign, Repository


class EnvironmentBuilder(Protocol):
    def __call__(
        self,
        episode_id: str,
        manifest: TargetManifest,
        policies: list[PolicyDocument],
        capsule_handle: CapsuleHandle | None,
        /,
    ) -> BlackBoxEnvironment: ...


class RuntimeLifecycle(Protocol):
    async def provision(
        self, episode_id: str, manifest: TargetManifest
    ) -> CapsuleHandle | None: ...

    async def destroy(self, handle: CapsuleHandle | None) -> None: ...


class NoopRuntimeLifecycle:
    """Explicit local-test lifecycle; production workers never choose it implicitly."""

    async def provision(self, episode_id: str, manifest: TargetManifest) -> None:
        return None

    async def healthcheck(self, handle: CapsuleHandle | None) -> bool:
        return True

    async def destroy(self, handle: CapsuleHandle | None) -> None:
        return None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _drain_private_trace(environment: BlackBoxEnvironment) -> dict[str, Any]:
    drain = getattr(environment, "drain_private_trace", None)
    if drain is None:
        return {}
    value = await _maybe_await(drain())
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"events": value}
    return {}


def _trace_graph(
    trace: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    effects: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    events = trace.get("events", [])
    if isinstance(events, list):
        for item in events:
            if not isinstance(item, dict):
                continue
            effect = item.get("effect")
            if isinstance(effect, dict):
                effects.append(
                    {
                        "effect": effect,
                        "decision": item.get("decision"),
                        "result": item.get("virtual_result", item.get("result", {})),
                    }
                )
            item_signals = item.get("verifier_signals", [])
            if isinstance(item_signals, list):
                signals.extend(value for value in item_signals if isinstance(value, dict))
    verifier = trace.get("verifier")
    if isinstance(verifier, dict) and isinstance(verifier.get("signals"), list):
        signals.extend(value for value in verifier["signals"] if isinstance(value, dict))

    unique: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for signal in signals:
        fingerprint = repr(sorted(signal.items()))
        if fingerprint not in fingerprints:
            fingerprints.add(fingerprint)
            unique.append(signal)
    return effects, unique


def record_public_reset(
    repository: Repository, episode_id: str, task: AttackTask, observation: PublicObservation
) -> None:
    """Retain the exact pre-action observation in the existing event store."""
    from .scenarios import content_hash

    loaded = repository.get_episode(episode_id)
    if loaded is None:
        raise ValueError("unknown episode")
    campaign = repository.get_campaign(loaded[0].campaign_id)
    assert campaign is not None
    experiment = (repository.get_red_experiment_config(campaign.red_config_id)
                  if campaign.red_config_id else None)
    repository.add_event("episode", episode_id, "EPISODE_PUBLIC_RESET", {
        "observation": observation.model_dump(mode="json"),
        "max_steps": task.max_steps_per_episode,
        "configuration_hash": content_hash({
            "task": task.model_dump(mode="json"), "campaign": campaign.configuration,
            "experiment": experiment.content_hash if experiment else None,
        }),
    })


class RepositoryTrajectorySink(TrajectorySink):
    """Atomically appends a public step and its private causal audit graph."""

    def __init__(
        self,
        repository: Repository,
        episode_id: str,
        *,
        environment: BlackBoxEnvironment | None = None,
        budget: BudgetTracker | None = None,
    ):
        self.repository = repository
        self.episode_id = episode_id
        self.environment = environment
        self.budget = budget

    async def record(self, action: RedAction, result: StepResult, step_index: int) -> None:
        if self.budget:
            self.budget.consume_step()
        trace = await _drain_private_trace(self.environment) if self.environment else {}
        effects, verifier_events = _trace_graph(trace)
        self.repository.record_step_graph(
            episode_id=self.episode_id,
            step_index=step_index,
            action=action.model_dump(mode="json"),
            observation=result.public_observation.model_dump(mode="json"),
            reward=result.reward,
            terminal=result.terminal_success,
            strategy_id=action.strategy_id,
            effects=effects,
            verifier_events=verifier_events,
            model_invocation_ids=trace.get("target_model_invocation_ids", []),
        )


class RepositoryStrategyMemory(StrategyMemory):
    def __init__(self, repository: Repository):
        self.repository = repository
        strategies: list[StrategyRecord] = []
        for row in repository.list_strategies():
            try:
                strategies.append(StrategyRecord.model_validate(row.document))
            except ValueError:
                continue
        super().__init__(strategies)

    def record_outcome(self, task: AttackTask, node: Any) -> None:
        super().record_outcome(task, node)
        for strategy in self._strategies:
            self.repository.save_strategy(strategy.strategy_id, strategy.model_dump(mode="json"))


class RepositorySearchObserver(SearchObserver):
    def __init__(
        self,
        repository: Repository,
        campaign_id: str,
        episode_ids: dict[int, str],
        model_invocation_ids: dict[tuple[str, str, int], list[str]] | None = None,
    ):
        self.repository = repository
        self.campaign_id = campaign_id
        self.episode_ids = episode_ids
        self.model_invocation_ids = model_invocation_ids if model_invocation_ids is not None else {}

    def record(self, event: SearchEvent) -> None:
        metadata = dict(event.metadata)
        planning_node_id = event.parent_id
        if event.event_type == "candidates_ranked" and event.node is not None:
            planning_node_id = event.node.trajectory_id
        proposed_step_index = metadata.get("proposed_step_index")
        invocation_ids: list[str] = []
        if (
            planning_node_id is not None
            and isinstance(proposed_step_index, int)
            and not isinstance(proposed_step_index, bool)
        ):
            invocation_ids = list(
                self.model_invocation_ids.get(
                    (event.search_id, planning_node_id, proposed_step_index), []
                )
            )
        if invocation_ids:
            metadata["model_invocation_ids"] = invocation_ids
        document = {
            "event_type": event.event_type,
            "search_id": event.search_id,
            "parent_id": event.parent_id,
            "episode_index": event.episode_index,
            "metadata": metadata,
            "node": event.node.model_dump(mode="json") if event.node else None,
        }
        if event.episode_index is not None:
            episode_id = self.episode_ids.get(event.episode_index)
            if episode_id:
                if invocation_ids:
                    candidate_step_index = metadata.get("candidate_step_index")
                    step_id: str | None = None
                    if isinstance(candidate_step_index, int) and not isinstance(
                        candidate_step_index, bool
                    ):
                        step = self.repository.get_episode_step(episode_id, candidate_step_index)
                        if step is None:
                            raise RuntimeError(
                                "adaptive model invocation step attribution is missing its step"
                            )
                        step_id = step.id
                    self.repository.link_model_invocations(
                        invocation_ids, episode_id, step_id=step_id
                    )
                if event.node is None:
                    self.repository.add_event(
                        "campaign",
                        self.campaign_id,
                        f"RED_{event.event_type.upper()}",
                        document,
                    )
                    return
                recorded_priority = metadata.get("priority")
                score = (
                    float(recorded_priority)
                    if isinstance(recorded_priority, int | float)
                    and not isinstance(recorded_priority, bool)
                    else event.node.cumulative_reward
                    + event.node.novelty_score
                    - event.node.model_cost
                )
                self.repository.add_trajectory_summary(episode_id, document, score)
                return
        self.repository.add_event(
            "campaign", self.campaign_id, f"RED_{event.event_type.upper()}", document
        )


class ManagedEpisodeEnvironment:
    """Owns one isolated episode and makes finalization plus teardown unavoidable."""

    def __init__(
        self,
        *,
        campaign: Campaign,
        episode_id: str,
        delegate: BlackBoxEnvironment,
        handle: CapsuleHandle | None,
        runtime: RuntimeLifecycle,
        repository: Repository,
        budget: BudgetTracker,
        artifact_store: ArtifactStore | None,
        auto_record: bool,
    ):
        self.campaign = campaign
        self.episode_id = episode_id
        self.delegate = delegate
        self.handle = handle
        self.runtime = runtime
        self.repository = repository
        self.artifact_store = artifact_store
        self.auto_record = auto_record
        self.step_index = 0
        self.cumulative_reward = 0.0
        self.terminal_success = False
        self.failed_error: str | None = None
        self.closed = False
        self._delegate_close_attempted = False
        self._finalized = False
        self._destroyed = False
        self._destruction_recorded = False
        self._pending_model_invocation_ids: dict[int, list[str]] = {}
        self.sink = RepositoryTrajectorySink(
            repository, episode_id, environment=delegate, budget=budget
        )

    async def reset(self, task: AttackTask) -> PublicObservation:
        try:
            observation = await self.delegate.reset(task)
            record_public_reset(self.repository, self.episode_id, task, observation)
            return observation
        except Exception as exc:
            self.failed_error = str(exc)
            raise

    async def step(self, action: RedAction) -> StepResult:
        self.step_index += 1
        try:
            result = await self.delegate.step(action)
            self.cumulative_reward += result.reward
            self.terminal_success = self.terminal_success or result.terminal_success
            if self.auto_record:
                await self.record(action, result, self.step_index)
            return result
        except Exception as exc:
            self.failed_error = str(exc)
            raise

    def queue_model_invocation(
        self, model_invocation_id: str, *, step_index: int | None = None
    ) -> None:
        # Persist branch-level attribution immediately so terminal prefix replays and
        # evidence generated during close still retain the planning invocation.
        self.repository.link_model_invocation(model_invocation_id, self.episode_id)
        target_step = step_index if step_index is not None else self.step_index + 1
        self._pending_model_invocation_ids.setdefault(target_step, []).append(model_invocation_id)

    async def record(self, action: RedAction, result: StepResult, step_index: int) -> None:
        await self.sink.record(action, result, step_index)
        invocation_ids = self._pending_model_invocation_ids.pop(step_index, [])
        if not invocation_ids:
            return
        step = self.repository.get_episode_step(self.episode_id, step_index)
        if step is None:
            raise RuntimeError("model invocation step attribution is missing its recorded step")
        self.repository.link_model_invocations(
            invocation_ids,
            self.episode_id,
            step_id=step.id,
        )

    async def account_model_tokens(self, tokens: int) -> None:
        account = getattr(self.delegate, "account_model_tokens", None)
        if account is not None:
            await _maybe_await(account(tokens))

    def drain_private_trace(self) -> dict[str, Any]:
        drain = getattr(self.delegate, "drain_private_trace", None)
        if drain is None:
            return {}
        value = drain()
        return value if isinstance(value, dict) else {}

    async def close(self) -> None:
        if self.closed:
            return
        errors: list[BaseException] = []
        if not self._delegate_close_attempted:
            self._delegate_close_attempted = True
            try:
                await self.delegate.close()
            except BaseException as exc:  # cleanup must continue through cancellation
                errors.append(exc)
                self.failed_error = self.failed_error or str(exc) or type(exc).__name__

        if not self._finalized:
            try:
                loaded = self.repository.get_episode(self.episode_id)
                if loaded and loaded[0].status == EpisodeStatus.EXECUTING:
                    self.repository.set_episode_status(self.episode_id, EpisodeStatus.VERIFYING)
                loaded = self.repository.get_episode(self.episode_id)
                if loaded and loaded[0].status == EpisodeStatus.VERIFYING:
                    status = (
                        EpisodeStatus.FAILED
                        if self.failed_error
                        else EpisodeStatus.SUCCEEDED
                        if self.terminal_success
                        else EpisodeStatus.EXHAUSTED
                    )
                    self.repository.set_episode_status(
                        self.episode_id,
                        status,
                        cumulative_reward=self.cumulative_reward,
                        terminal_success=self.terminal_success,
                        error=self.failed_error,
                        completed_at=datetime.now(UTC),
                    )
                loaded = self.repository.get_episode(self.episode_id)
                if loaded and loaded[0].terminal_success:
                    self._record_terminal_findings()
                if self.artifact_store:
                    existing = self.repository.list_artifacts(
                        episode_id=self.episode_id,
                        kind="episode_evidence",
                    )
                    if existing:
                        self.repository.link_episode_findings_to_evidence(
                            self.episode_id, existing[0].id
                        )
                    else:
                        EvidenceBuilder(self.repository, self.artifact_store).build_episode_bundle(
                            self.campaign.id, self.episode_id
                        )
                self._finalized = True
            except BaseException as exc:
                errors.append(exc)

        if not self._destroyed:
            try:
                await self.runtime.destroy(self.handle)
                self._destroyed = True
            except BaseException as exc:
                errors.append(exc)
        if self._destroyed and not self._destruction_recorded:
            try:
                loaded = self.repository.get_episode(self.episode_id)
                if loaded is None or loaded[0].status == EpisodeStatus.DESTROYED:
                    self._destruction_recorded = True
                elif loaded[0].status in {
                    EpisodeStatus.SUCCEEDED,
                    EpisodeStatus.EXHAUSTED,
                    EpisodeStatus.FAILED,
                }:
                    self.repository.set_episode_status(self.episode_id, EpisodeStatus.DESTROYED)
                    self._destruction_recorded = True
            except BaseException as exc:
                errors.append(exc)
        self.closed = self._finalized and self._destroyed and self._destruction_recorded
        if errors:
            raise errors[0]

    def _record_terminal_findings(self) -> None:
        task = AttackTask.model_validate(self.repository.load_task(self.campaign.attack_task_id))
        specifications = {spec.verifier_id: spec for spec in task.forbidden_states}
        terminal: dict[str, float] = {}
        for row in self.repository.list_verifier_events(self.episode_id):
            try:
                signal = VerifierSignal.model_validate(row.document)
            except ValueError:
                continue
            if not signal.terminal_success:
                continue
            if signal.verifier_id not in specifications:
                raise RuntimeError("terminal verifier signal is not declared by the attack task")
            terminal[signal.verifier_id] = max(
                terminal.get(signal.verifier_id, 0.0), signal.severity
            )

        if not terminal:
            # Contract-only environments may expose terminal success without a private
            # signal stream. A single task verifier remains an unambiguous fallback;
            # multi-verifier tasks fail closed instead of inventing an identity.
            if len(specifications) != 1:
                raise RuntimeError("terminal episode lacks verifier identity evidence")
            verifier_id, specification = next(iter(specifications.items()))
            terminal[verifier_id] = specification.severity

        existing = {
            finding.verifier_id
            for finding in self.repository.list_findings(episode_id=self.episode_id)
        }
        for verifier_id, severity in sorted(terminal.items()):
            if verifier_id not in existing:
                self.repository.add_finding(
                    self.campaign.id,
                    self.episode_id,
                    verifier_id,
                    severity,
                )


class CampaignRunner:
    def __init__(
        self,
        repository: Repository,
        model: AttackerModel,
        environment_builder: EnvironmentBuilder,
        runtime: RuntimeLifecycle | None = None,
        *,
        artifact_store: ArtifactStore | None = None,
        experiment_tracker: Any | None = None,
        effective_model_config: ModelConfig | None = None,
    ):
        self.repository = repository
        self.model = model
        self.environment_builder = environment_builder
        self.runtime = runtime or NoopRuntimeLifecycle()
        self.artifact_store = artifact_store
        self.experiment_tracker = experiment_tracker
        self.effective_model_config = effective_model_config
        self._active_episode: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "active_episode", default=None
        )
        self._active_environment: contextvars.ContextVar[ManagedEpisodeEnvironment | None] = (
            contextvars.ContextVar("active_environment", default=None)
        )

    async def run(self, campaign_id: str) -> None:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError("campaign not found")
        if campaign.status in {
            CampaignStatus.COMPLETED,
            CampaignStatus.FAILED,
            CampaignStatus.REJECTED,
            CampaignStatus.BLOCKED,
        }:
            return
        if campaign.cancellation_requested or campaign.status == CampaignStatus.CANCELLING:
            self.repository.set_campaign_status(
                campaign_id, CampaignStatus.FAILED, completed_at=datetime.now(UTC)
            )
            return
        if campaign.status == CampaignStatus.CREATED:
            self.repository.set_campaign_status(campaign_id, CampaignStatus.VALIDATING)

        task = AttackTask.model_validate(self.repository.load_task(campaign.attack_task_id))
        manifest = TargetManifest.model_validate(
            self.repository.load_manifest(campaign.target_version_id)
        )
        policies = [
            PolicyDocument.model_validate(item)
            for item in self.repository.policy_documents(campaign.policy_version_id)
        ]
        preflight = containment_preflight(
            CapsuleSpec(
                episode_id="preflight",
                image=manifest.image,
                entrypoint=manifest.entrypoint,
                environment=manifest.environment_aliases,
                resource_limits=manifest.resource_limits,
                allowed_destination_aliases=list(manifest.destination_routes),
            )
        )
        self.repository.record_containment_preflight(campaign_id, preflight)
        if not preflight.verified:
            self.repository.set_campaign_status(
                campaign_id, CampaignStatus.REJECTED, completed_at=datetime.now(UTC)
            )
            return

        prepare = getattr(self.runtime, "prepare", None)
        if prepare is not None:
            try:
                readiness = await prepare(manifest)
                self.repository.add_event("campaign", campaign_id, "CAMPAIGN_IMAGE_READINESS", readiness.model_dump(mode="json"))
            except ImageUnavailable as exc:
                self._block_for_image(campaign_id, exc)
                return

        campaign_at_start = campaign
        try:
            experiment = self._experiment(campaign_at_start)
            self._validate_model_config(experiment.model)
        except ValueError:
            self.repository.set_campaign_status(
                campaign_id, CampaignStatus.REJECTED, completed_at=datetime.now(UTC)
            )
            raise
        budget = BudgetTracker(
            task,
            episodes=campaign_at_start.episodes_started,
            tokens=campaign_at_start.tokens_used,
            cost=campaign_at_start.cost_used,
        )
        adaptive_invocation_ids: dict[tuple[str, str, int], list[str]] = {}

        def consume_usage(usage: ModelUsage) -> None:
            episode_id = self._active_episode.get()
            request_hash = (
                usage.request_hash
                or hashlib.sha256(
                    (
                        f"{campaign_at_start.id}:{usage.operation}:{usage.provider}:{usage.model}:"
                        f"{campaign_at_start.tokens_used}:{campaign_at_start.cost_used}"
                    ).encode()
                ).hexdigest()
            )
            invocation = self.repository.add_campaign_usage(
                campaign_at_start.id,
                episode_id=episode_id,
                provider=usage.provider,
                model=usage.model,
                tokens=usage.tokens,
                cost=usage.cost,
                latency_ms=usage.latency_ms,
                request_hash=request_hash,
                status=(
                    "FAILED"
                    if usage.successful_calls == 0 and usage.failed_calls
                    else "SUCCEEDED_WITH_RETRIES"
                    if usage.failed_calls
                    else "SUCCEEDED"
                ),
                configuration={
                    "execution_kind": usage.execution_kind,
                    "usage_source": usage.usage_source,
                    "cost_source": usage.cost_source,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "operation": usage.operation,
                    "successful_calls": usage.successful_calls,
                    "failed_calls": usage.failed_calls,
                    "red_config_id": campaign_at_start.red_config_id,
                    "search_id": usage.search_id,
                    "search_node_id": usage.search_node_id,
                    "proposed_step_index": usage.step_index,
                },
            )
            if (
                usage.search_id is not None
                and usage.search_node_id is not None
                and usage.step_index is not None
            ):
                key = (usage.search_id, usage.search_node_id, usage.step_index)
                adaptive_invocation_ids.setdefault(key, []).append(invocation.id)
            active_environment = self._active_environment.get()
            if active_environment is not None:
                active_environment.queue_model_invocation(invocation.id)
            budget.consume_model(usage.tokens, usage.cost)

        model = BudgetedAttackerModel(self.model, consume_usage)
        latest = self.repository.get_campaign(campaign_id)
        if latest and latest.status == CampaignStatus.VALIDATING:
            self.repository.set_campaign_status(campaign_id, CampaignStatus.READY)
        if self._cancelled(campaign_id):
            self.repository.cancel_campaign(campaign_id)
            self._fail_campaign(campaign_id)
            return
        latest = self.repository.get_campaign(campaign_id)
        if latest and latest.status == CampaignStatus.READY:
            self.repository.set_campaign_status(
                campaign_id, CampaignStatus.RUNNING, started_at=datetime.now(UTC)
            )
        active_campaign = self.repository.get_campaign(campaign_id)
        if active_campaign is None:
            raise KeyError("campaign disappeared during validation")
        now = datetime.now(UTC)
        wall_time_origin = active_campaign.started_at or active_campaign.created_at
        if wall_time_origin.tzinfo is None:
            wall_time_origin = wall_time_origin.replace(tzinfo=UTC)
        else:
            wall_time_origin = wall_time_origin.astimezone(UTC)
        remaining_wall_time = task.max_wall_time_seconds - max(
            0.0, (now - wall_time_origin).total_seconds()
        )
        if remaining_wall_time <= 0:
            raise TimeoutError("campaign wall-time budget is exhausted")

        try:
            async with asyncio.timeout(remaining_wall_time):
                if active_campaign.run_kind == "BENIGN_REGRESSION":
                    await self._run_benign(
                        active_campaign, task, manifest, policies, budget, experiment
                    )
                elif (
                    active_campaign.replay_episode_id or active_campaign.run_kind == "EXACT_REPLAY"
                ):
                    await self._run_exact_replay(
                        active_campaign, task, manifest, policies, budget, experiment
                    )
                elif active_campaign.search_mode == "adaptive":
                    await self._run_adaptive(
                        active_campaign,
                        task,
                        manifest,
                        policies,
                        model,
                        budget,
                        experiment,
                        adaptive_invocation_ids,
                    )
                else:
                    await self._run_linear(
                        active_campaign,
                        task,
                        manifest,
                        policies,
                        model,
                        budget,
                        experiment,
                    )
            if self._cancelled(campaign_id):
                self._fail_campaign(campaign_id)
            else:
                self.repository.set_campaign_status(
                    campaign_id, CampaignStatus.COMPLETED, completed_at=datetime.now(UTC)
                )
                await self._finalize_hardening_runs(campaign_id)
            await self._record_experiment(campaign_id, experiment)
        except ImageUnavailable as exc:
            self._block_for_image(campaign_id, exc)
        except asyncio.CancelledError:
            # API cancellation is durable and terminal; infrastructure cancellation
            # (worker shutdown or lease loss) must remain retryable.
            if self._cancelled(campaign_id):
                self._fail_campaign(campaign_id)
                return
            raise
        except Exception:
            # The leased worker owns retry policy and reconciles the campaign only
            # after max attempts. Leaving RUNNING here preserves crash recovery.
            raise

    def _block_for_image(self, campaign_id: str, exc: ImageUnavailable) -> None:
        self.repository.add_event("campaign", campaign_id, "CAMPAIGN_IMAGE_READINESS", exc.readiness.model_dump(mode="json"))
        if self._cancelled(campaign_id):
            self._fail_campaign(campaign_id)
        else:
            self.repository.set_campaign_status(campaign_id, CampaignStatus.BLOCKED, completed_at=datetime.now(UTC))

    def _experiment(self, campaign: Campaign) -> RedExperimentConfig:
        if campaign.red_config_id:
            stored = self.repository.get_red_experiment_config(campaign.red_config_id)
            if stored:
                try:
                    return RedExperimentConfig.model_validate(
                        {**stored.document, "config_id": stored.id}
                    )
                except ValueError:
                    pass
        return RedExperimentConfig()

    def _validate_model_config(self, requested: ModelConfig) -> None:
        """Bind campaign configuration to the operator-controlled endpoint and credential."""

        effective = self.effective_model_config
        if effective is None:
            return
        requested_document = requested.model_dump(mode="json")
        effective_document = effective.model_dump(mode="json")
        mismatches = sorted(
            key
            for key in requested_document
            if requested_document.get(key) != effective_document.get(key)
        )
        if mismatches:
            raise ValueError(
                "Red experiment model configuration is not permitted by operator settings: "
                + ", ".join(mismatches)
            )

    def _cancelled(self, campaign_id: str) -> bool:
        row = self.repository.get_campaign(campaign_id)
        return row is None or bool(row.cancellation_requested)

    def _fail_campaign(self, campaign_id: str) -> None:
        latest = self.repository.get_campaign(campaign_id)
        if latest and latest.status not in {
            CampaignStatus.COMPLETED,
            CampaignStatus.FAILED,
            CampaignStatus.REJECTED,
            CampaignStatus.BLOCKED,
        }:
            self.repository.set_campaign_status(
                campaign_id, CampaignStatus.FAILED, completed_at=datetime.now(UTC)
            )

    async def _new_environment(
        self,
        campaign: Campaign,
        manifest: TargetManifest,
        policies: list[PolicyDocument],
        budget: BudgetTracker,
        *,
        seed: int,
        auto_record: bool,
        experiment: RedExperimentConfig,
    ) -> ManagedEpisodeEnvironment:
        if self._cancelled(campaign.id):
            raise asyncio.CancelledError
        budget.start_episode()
        episode = self.repository.create_episode(campaign.id, seed)
        self.repository.set_episode_status(
            episode.id, EpisodeStatus.PROVISIONING, started_at=datetime.now(UTC)
        )
        handle: CapsuleHandle | None = None
        delegate: BlackBoxEnvironment | None = None
        try:
            handle = await self.runtime.provision(episode.id, manifest)
            if handle is not None and handle.runtime_containment_proof is not None:
                self.repository.record_runtime_containment(
                    episode.id,
                    handle.runtime_containment_proof,
                )
            # Resolve the immutable version at episode bootstrap. A later policy
            # version can therefore be selected by a new campaign without mutating
            # the PolicyEngine already pinned inside any active episode.
            episode_policies = (
                [
                    PolicyDocument.model_validate(document)
                    for document in self.repository.policy_documents(campaign.policy_version_id)
                ]
                if campaign.policy_version_id is not None
                else policies
            )
            delegate = self.environment_builder(episode.id, manifest, episode_policies, handle)
            prepare = getattr(delegate, "prepare", None)
            if prepare is not None:
                await _maybe_await(prepare(budget.task.model_copy(update={"random_seed": seed})))
            healthcheck = getattr(self.runtime, "healthcheck", None)
            deadline = asyncio.get_running_loop().time() + (
                min(30, manifest.resource_limits.timeout_seconds) if prepare is not None else 0
            )
            while healthcheck is not None and not await _maybe_await(healthcheck(handle)):
                if asyncio.get_running_loop().time() >= deadline:
                    raise CapsuleError("capsule failed trusted readiness checks")
                await asyncio.sleep(0.2)
            self.repository.set_episode_status(episode.id, EpisodeStatus.READY)
            configure_reward = getattr(delegate, "configure_reward", None)
            if configure_reward is not None:
                reward_config = experiment.reward
                if not experiment.ablation.uses_novelty:
                    reward_config = reward_config.model_copy(update={"novelty_weight": 0})
                await _maybe_await(configure_reward(reward_config))
            configure_novelty = getattr(delegate, "configure_novelty", None)
            if configure_novelty is not None:
                await _maybe_await(configure_novelty(experiment.novelty))
            self.repository.set_episode_status(episode.id, EpisodeStatus.EXECUTING)
            return ManagedEpisodeEnvironment(
                campaign=campaign,
                episode_id=episode.id,
                delegate=delegate,
                handle=handle,
                runtime=self.runtime,
                repository=self.repository,
                budget=budget,
                artifact_store=self.artifact_store,
                auto_record=auto_record,
            )
        except BaseException as exc:
            if delegate is not None:
                try:
                    await delegate.close()
                except Exception:
                    pass
            persistence_error: BaseException | None = None
            try:
                loaded = self.repository.get_episode(episode.id)
                if loaded and loaded[0].status in {
                    EpisodeStatus.PENDING,
                    EpisodeStatus.PROVISIONING,
                    EpisodeStatus.READY,
                    EpisodeStatus.EXECUTING,
                }:
                    self.repository.set_episode_status(
                        episode.id,
                        EpisodeStatus.FAILED,
                        error=str(exc) or type(exc).__name__,
                        completed_at=datetime.now(UTC),
                    )
            except BaseException as status_exc:
                persistence_error = status_exc
            try:
                await self.runtime.destroy(handle)
            except BaseException as cleanup_exc:
                raise cleanup_exc from exc
            try:
                loaded = self.repository.get_episode(episode.id)
                if loaded and loaded[0].status == EpisodeStatus.FAILED:
                    self.repository.set_episode_status(episode.id, EpisodeStatus.DESTROYED)
            except BaseException as status_exc:
                persistence_error = persistence_error or status_exc
            if persistence_error is not None:
                raise persistence_error from exc
            raise

    async def _run_linear(
        self,
        campaign: Campaign,
        task: AttackTask,
        manifest: TargetManifest,
        policies: list[PolicyDocument],
        model: AttackerModel,
        budget: BudgetTracker,
        experiment: RedExperimentConfig,
    ) -> None:
        memory = RepositoryStrategyMemory(self.repository)
        prior = self.repository.list_episodes(campaign.id)
        for index in range(len(prior), task.max_episodes):
            if self._cancelled(campaign.id):
                break
            environment = await self._new_environment(
                campaign,
                manifest,
                policies,
                budget,
                seed=task.random_seed + index,
                auto_record=False,
                experiment=experiment,
            )
            token = self._active_episode.set(environment.episode_id)
            environment_token = self._active_environment.set(environment)
            try:
                run_task = task.model_copy(update={"random_seed": task.random_seed + index})
                result = await LinearSearch(
                    model,
                    memory,
                    experiment=experiment,
                    cancellation_requested=lambda: self._cancelled(campaign.id),
                ).run(run_task, environment, environment)
                if result.cancelled or result.node.terminal_success:
                    break
            except Exception as exc:
                environment.failed_error = str(exc)
                raise
            finally:
                self._active_environment.reset(environment_token)
                self._active_episode.reset(token)
                await environment.close()

    async def _run_adaptive(
        self,
        campaign: Campaign,
        task: AttackTask,
        manifest: TargetManifest,
        policies: list[PolicyDocument],
        model: AttackerModel,
        budget: BudgetTracker,
        experiment: RedExperimentConfig,
        model_invocation_ids: dict[tuple[str, str, int], list[str]],
    ) -> None:
        remaining_episodes = task.max_episodes - campaign.episodes_started
        if remaining_episodes <= 0:
            return
        run_task = task.model_copy(
            update={
                "max_episodes": remaining_episodes,
                "random_seed": task.random_seed + campaign.episodes_started,
            }
        )
        episode_ids: dict[int, str] = {}

        async def factory(request: BranchRequest) -> BlackBoxEnvironment:
            environment = await self._new_environment(
                campaign,
                manifest,
                policies,
                budget,
                seed=request.seed,
                auto_record=True,
                experiment=experiment,
            )
            episode_ids[request.attempt_index] = environment.episode_id
            for invocation_id in model_invocation_ids.get(
                (
                    request.search_id,
                    request.parent_trajectory_id,
                    request.proposed_step_index,
                ),
                [],
            ):
                environment.queue_model_invocation(
                    invocation_id,
                    step_index=request.proposed_step_index,
                )
            return environment

        source_actions: list[RedAction] = []
        if campaign.run_kind == "NEARBY_BYPASS" and campaign.source_episode_id:
            source = self.repository.get_episode(campaign.source_episode_id)
            if source:
                source_actions = [RedAction.model_validate(step.red_action) for step in source[1]]
        result = await BestFirstBeamSearch(
            model,
            factory,
            memory=RepositoryStrategyMemory(self.repository),
            search_config=experiment.search.model_copy(
                update={
                    "max_concurrency": min(experiment.search.max_concurrency, task.max_concurrency)
                }
            ),
            experiment=experiment,
            observer=RepositorySearchObserver(
                self.repository,
                campaign.id,
                episode_ids,
                model_invocation_ids,
            ),
            cancellation_requested=lambda: self._cancelled(campaign.id),
            source_actions=source_actions,
        ).run(run_task)
        if result.cancelled:
            self.repository.cancel_campaign(campaign.id)

    async def _run_exact_replay(
        self,
        campaign: Campaign,
        task: AttackTask,
        manifest: TargetManifest,
        policies: list[PolicyDocument],
        budget: BudgetTracker,
        experiment: RedExperimentConfig,
    ) -> None:
        source_id = campaign.replay_episode_id or campaign.source_episode_id
        if self.repository.list_episodes(campaign.id):
            return
        source = self.repository.get_episode(str(source_id))
        if source is None:
            raise KeyError("replay source episode not found")
        source_episode, source_steps = source
        environment = await self._new_environment(
            campaign,
            manifest,
            policies,
            budget,
            seed=source_episode.seed,
            auto_record=False,
            experiment=experiment,
        )
        replay_task = task.model_copy(update={"random_seed": source_episode.seed})
        try:
            await environment.reset(replay_task)
            for index, source_step in enumerate(source_steps, 1):
                if self._cancelled(campaign.id):
                    break
                action = RedAction.model_validate(source_step.red_action)
                result = await environment.step(action)
                await environment.sink.record(action, result, index)
                if result.done:
                    break
        except Exception as exc:
            environment.failed_error = str(exc)
            raise
        finally:
            await environment.close()

    async def _run_benign(
        self,
        campaign: Campaign,
        task: AttackTask,
        manifest: TargetManifest,
        policies: list[PolicyDocument],
        budget: BudgetTracker,
        experiment: RedExperimentConfig,
    ) -> None:
        raw_actions = campaign.configuration.get("benign_actions", [])
        raw_expectations = campaign.configuration.get("benign_expectations", [])
        if self.repository.list_episodes(campaign.id):
            return
        if not isinstance(raw_actions, list) or not raw_actions:
            raise ValueError("benign regression campaign requires benign_actions")
        if not isinstance(raw_expectations, list) or not raw_expectations:
            raise ValueError("benign regression campaign requires benign_expectations")
        actions = [RedAction.model_validate(item) for item in raw_actions]
        expectations = [BenignExpectation.model_validate(item) for item in raw_expectations]
        if max(item.action_index for item in expectations) > len(actions):
            raise ValueError("benign expectation action_index exceeds benign_actions")
        environment = await self._new_environment(
            campaign,
            manifest,
            policies,
            budget,
            seed=task.random_seed,
            auto_record=False,
            experiment=experiment,
        )
        no_visible_errors = True
        no_forbidden_state = True
        try:
            await environment.reset(task)
            for index, action in enumerate(actions, 1):
                result = await environment.step(action)
                await environment.sink.record(action, result, index)
                no_visible_errors = (
                    no_visible_errors and not result.public_observation.visible_errors
                )
                no_forbidden_state = no_forbidden_state and not result.terminal_success
                if result.done:
                    break
            expectation_proofs = self._benign_expectation_proofs(
                environment.episode_id, expectations
            )
            passed = (
                no_visible_errors
                and no_forbidden_state
                and bool(expectation_proofs)
                and all(proof["passed"] is True for proof in expectation_proofs)
            )
            self.repository.add_trajectory_summary(
                environment.episode_id,
                {
                    "kind": "benign_regression",
                    "passed": passed,
                    "safety": {
                        "no_visible_errors": no_visible_errors,
                        "no_forbidden_state": no_forbidden_state,
                    },
                    "expectations": expectation_proofs,
                },
                1.0 if passed else 0.0,
            )
        except Exception as exc:
            environment.failed_error = str(exc)
            raise
        finally:
            await environment.close()

    def _benign_expectation_proofs(
        self,
        episode_id: str,
        expectations: list[BenignExpectation],
    ) -> list[dict[str, Any]]:
        loaded = self.repository.get_episode(episode_id)
        if loaded is None:
            raise KeyError("benign regression episode not found")
        _, steps = loaded
        steps_by_index = {step.step_index: step for step in steps}
        effects = self.repository.list_effect_attempts(episode_id)
        effects_by_step: dict[str, list[Any]] = {}
        for effect in effects:
            if effect.step_id is not None:
                effects_by_step.setdefault(effect.step_id, []).append(effect)
        decisions = {
            decision.effect_id: decision
            for decision in self.repository.list_policy_decisions(episode_id)
        }
        proofs: list[dict[str, Any]] = []
        for expectation in expectations:
            step = steps_by_index.get(expectation.action_index)
            target_response = (
                step.public_observation.get("target_response") if step is not None else None
            )
            observation_matched: bool | None = None
            if expectation.target_response_contains is not None:
                observation_matched = (
                    isinstance(target_response, str)
                    and expectation.target_response_contains in target_response
                )

            matched_effects: list[dict[str, Any]] = []
            effect_matched: bool | None = None
            if expectation.effect_operation is not None:
                effect_matched = False
                for effect in effects_by_step.get(step.id if step is not None else "", []):
                    document = effect.document
                    if document.get("operation") != expectation.effect_operation:
                        continue
                    if (
                        expectation.effect_destination_alias is not None
                        and document.get("destination_alias")
                        != expectation.effect_destination_alias
                    ):
                        continue
                    decision = decisions.get(effect.id)
                    decision_value = (
                        str(decision.document.get("decision")) if decision is not None else None
                    )
                    result_success = bool(
                        decision is not None and decision.result.get("success") is True
                    )
                    permitted = decision_value in {
                        str(Decision.SIMULATE),
                        str(Decision.TRANSFORM),
                    }
                    matched_effects.append(
                        {
                            "effect_id": effect.id,
                            "destination_alias": document.get("destination_alias"),
                            "operation": document.get("operation"),
                            "decision": decision_value,
                            "policy_ids": (
                                decision.document.get("policy_ids", [])
                                if decision is not None
                                else []
                            ),
                            "result_success": result_success,
                            "permitted": permitted,
                        }
                    )
                    effect_matched = effect_matched or (permitted and result_success)

            checks = [check for check in (observation_matched, effect_matched) if check is not None]
            proofs.append(
                {
                    "action_index": expectation.action_index,
                    "passed": bool(checks) and all(checks),
                    "observation": (
                        {
                            "target_response_contains": expectation.target_response_contains,
                            "actual_target_response": target_response,
                            "matched": observation_matched,
                        }
                        if expectation.target_response_contains is not None
                        else None
                    ),
                    "effect": (
                        {
                            "destination_alias": expectation.effect_destination_alias,
                            "operation": expectation.effect_operation,
                            "matched_and_permitted": effect_matched,
                            "matches": matched_effects,
                        }
                        if expectation.effect_operation is not None
                        else None
                    ),
                }
            )
        return proofs

    async def _record_experiment(self, campaign_id: str, experiment: RedExperimentConfig) -> None:
        if self.experiment_tracker is None:
            return
        record = getattr(self.experiment_tracker, "record_campaign", None)
        if record:
            try:
                await _maybe_await(
                    record(
                        campaign_id=campaign_id,
                        config=experiment.model_dump(mode="json"),
                        metrics=self.repository.campaign_metrics(campaign_id),
                    )
                )
            except Exception as exc:
                self.repository.add_event(
                    "campaign",
                    campaign_id,
                    "EXPERIMENT_TRACKING_FAILED",
                    {"error_type": type(exc).__name__},
                )

    async def _finalize_hardening_runs(self, campaign_id: str) -> None:
        """Close a hardening loop once every requested child campaign is terminal."""

        terminal = {
            CampaignStatus.COMPLETED,
            CampaignStatus.FAILED,
            CampaignStatus.REJECTED,
            CampaignStatus.BLOCKED,
        }
        for run in self.repository.list_hardening_runs_for_campaign(campaign_id):
            if run.status != "PENDING":
                continue
            campaign_ids = {
                "exact_replay": run.exact_replay_campaign_id,
                "nearby_bypass": run.bypass_campaign_id,
                "benign_regression": run.benign_campaign_id,
            }
            campaigns = {
                kind: self.repository.get_campaign(child_id) if child_id else None
                for kind, child_id in campaign_ids.items()
            }
            requested = {kind: row for kind, row in campaigns.items() if campaign_ids[kind]}
            if not requested or any(row is None for row in requested.values()):
                continue
            if any(row.status not in terminal for row in requested.values() if row is not None):
                continue

            metrics = {
                kind: self.repository.campaign_metrics(row.id)
                for kind, row in requested.items()
                if row is not None
            }
            all_completed = all(
                row is not None and row.status == CampaignStatus.COMPLETED
                for row in requested.values()
            )
            exact_proof = (
                self._exact_replay_policy_proof(run, campaigns["exact_replay"])
                if run.exact_replay_campaign_id
                else None
            )
            exact_blocked = exact_proof["passed"] if exact_proof is not None else None
            bypass_proof: dict[str, Any] | None = None
            bypass_blocked: bool | None = None
            if run.bypass_campaign_id:
                bypass_metrics = metrics["nearby_bypass"]
                variants_tested = (
                    bypass_metrics["episodes_recorded"] > 0 and bypass_metrics["step_count"] > 0
                )
                bypass_blocked = variants_tested and bypass_metrics["finding_count"] == 0
                bypass_proof = {
                    "passed": bypass_blocked,
                    "variants_tested": variants_tested,
                    "episode_count": bypass_metrics["episodes_recorded"],
                    "step_count": bypass_metrics["step_count"],
                    "finding_count": bypass_metrics["finding_count"],
                }
            benign_passed: bool | None = None
            benign_proof: dict[str, Any] | None = None
            if run.benign_campaign_id:
                benign_summaries = [
                    summary.document
                    for episode in self.repository.list_episodes(run.benign_campaign_id)
                    for summary in self.repository.list_trajectory_summaries(episode.id)
                    if summary.document.get("kind") == "benign_regression"
                ]
                benign_passed = bool(benign_summaries) and all(
                    summary.get("passed") is True for summary in benign_summaries
                )
                benign_proof = {
                    "passed": benign_passed,
                    "episode_proofs": benign_summaries,
                }

            checks = [
                check
                for check in (exact_blocked, bypass_blocked, benign_passed)
                if check is not None
            ]
            passed = all_completed and bool(checks) and all(checks)
            bypass_observed = bool(
                (exact_proof and exact_proof["finding_count"] > 0)
                or (bypass_proof and bypass_proof["finding_count"] > 0)
            )
            if passed:
                status = "PASSED"
            elif all_completed and bypass_observed:
                status = "BYPASS_FOUND"
            else:
                status = "FAILED"
            result = {
                "exact_attack_blocked": exact_blocked,
                "nearby_bypasses_blocked": bypass_blocked,
                "benign_behavior_passed": benign_passed,
                "campaigns": metrics,
                "proof": {
                    "exact_replay": exact_proof,
                    "nearby_bypass": bypass_proof,
                    "benign_regression": benign_proof,
                },
            }
            self.repository.complete_hardening_run(run.id, status=status, result=result)
            if self.artifact_store is not None:
                try:
                    EvidenceBuilder(self.repository, self.artifact_store).build_hardening_bundle(
                        run.id
                    )
                except Exception as exc:
                    self.repository.add_event(
                        "hardening_run",
                        run.id,
                        "HARDENING_EVIDENCE_FAILED",
                        {"error_type": type(exc).__name__},
                    )

    def _exact_replay_policy_proof(
        self,
        run: Any,
        replay_campaign: Campaign | None,
    ) -> dict[str, Any]:
        finding = self.repository.get_finding(run.finding_id)
        if finding is None:
            raise KeyError("hardening source finding not found")
        policy = self.repository.get_policy_version(run.policy_version_id)
        if policy is None:
            raise KeyError("hardening policy version not found")

        source_effect_refs: set[str] = set()
        for event in self.repository.list_verifier_events(finding.episode_id):
            document = event.document
            if (
                document.get("verifier_id") == finding.verifier_id
                and document.get("terminal_success") is True
            ):
                refs = document.get("evidence_refs", [])
                if isinstance(refs, list):
                    source_effect_refs.update(str(ref) for ref in refs)
        source_effects = {
            effect.id: effect for effect in self.repository.list_effect_attempts(finding.episode_id)
        }
        source_selectors = sorted(
            {
                (
                    str(source_effects[effect_id].document.get("destination_alias")),
                    str(source_effects[effect_id].document.get("operation")),
                )
                for effect_id in source_effect_refs
                if effect_id in source_effects
            }
        )

        stored_policy_ids = {
            str(document.get("policy_id"))
            for document in policy.policies
            if document.get("policy_id")
        }
        matched_blocks: list[dict[str, Any]] = []
        replay_episode_ids: list[str] = []
        if replay_campaign is not None:
            for episode in self.repository.list_episodes(replay_campaign.id):
                replay_episode_ids.append(episode.id)
                decisions = {
                    decision.effect_id: decision
                    for decision in self.repository.list_policy_decisions(episode.id)
                }
                for effect in self.repository.list_effect_attempts(episode.id):
                    selector = (
                        str(effect.document.get("destination_alias")),
                        str(effect.document.get("operation")),
                    )
                    if selector not in source_selectors:
                        continue
                    decision = decisions.get(effect.id)
                    if decision is None:
                        continue
                    decision_value = str(decision.document.get("decision"))
                    raw_policy_ids = decision.document.get("policy_ids", [])
                    decision_policy_ids = (
                        {str(value) for value in raw_policy_ids}
                        if isinstance(raw_policy_ids, list)
                        else set()
                    )
                    attributed_to_version = bool(decision_policy_ids) and (
                        not stored_policy_ids or bool(decision_policy_ids & stored_policy_ids)
                    )
                    result_blocked = decision.result.get("success") is False
                    if (
                        decision_value in {str(Decision.DENY), str(Decision.REQUIRE_APPROVAL)}
                        and attributed_to_version
                        and result_blocked
                    ):
                        matched_blocks.append(
                            {
                                "episode_id": episode.id,
                                "effect_id": effect.id,
                                "destination_alias": selector[0],
                                "operation": selector[1],
                                "decision": decision_value,
                                "policy_ids": sorted(decision_policy_ids),
                                "reason_code": decision.document.get("reason_code"),
                                "result_success": decision.result.get("success"),
                            }
                        )

        finding_count = (
            len(self.repository.list_findings(campaign_id=replay_campaign.id))
            if replay_campaign is not None
            else 0
        )
        campaign_completed = bool(
            replay_campaign is not None and replay_campaign.status == CampaignStatus.COMPLETED
        )
        passed = bool(
            campaign_completed and source_selectors and matched_blocks and finding_count == 0
        )
        reasons: list[str] = []
        if not campaign_completed:
            reasons.append("exact_replay_campaign_not_completed")
        if not source_selectors:
            reasons.append("source_terminal_effect_evidence_missing")
        if source_selectors and not matched_blocks:
            reasons.append("no_attributed_blue_policy_block")
        if finding_count:
            reasons.append("forbidden_state_reproduced")
        return {
            "passed": passed,
            "campaign_completed": campaign_completed,
            "source_finding_id": finding.id,
            "source_verifier_id": finding.verifier_id,
            "source_effect_refs": sorted(source_effect_refs),
            "source_effect_selectors": [
                {"destination_alias": destination, "operation": operation}
                for destination, operation in source_selectors
            ],
            "replay_episode_ids": replay_episode_ids,
            "matched_policy_blocks": matched_blocks,
            "finding_count": finding_count,
            "reasons": reasons,
        }

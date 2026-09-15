from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from .artifacts import ArtifactStore, create_artifact_store, verify_artifact
from .contracts import (
    AttackTask,
    BenignExpectation,
    CampaignCreate,
    Decision,
    PolicyDocument,
    RedAction,
    TargetManifest,
)
from .evidence import EvidenceBuilder
from .red_contracts import RedExperimentConfig
from .scenarios import ScenarioCatalog
from .settings import Settings, get_settings
from .storage import Database, Repository
from .telemetry import configure_telemetry, instrument_fastapi, instrument_sqlalchemy


class TargetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class TargetVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str
    image_digest: str | None = None
    manifest: TargetManifest


class PolicyVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policies: list[PolicyDocument]

    @model_validator(mode="after")
    def validate_capsule_policy_bundle(self) -> PolicyVersionCreate:
        if any(policy.decision == Decision.ALLOW_REAL for policy in self.policies):
            raise ValueError("ALLOW_REAL is invalid for capsule policy versions")
        policy_ids = [policy.policy_id for policy in self.policies]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("policy_id values must be unique within a policy version")
        return self


class ReplayCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version_id: str | None = None
    search_nearby_bypasses: bool = False
    reproduction_only: bool = False


class RedExperimentConfigCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    document: dict[str, Any]


class HardeningCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version_id: str
    include_exact_replay: bool = True
    include_nearby_bypass_search: bool = True
    benign_attack_task_id: str | None = None
    benign_actions: list[RedAction] = Field(default_factory=list)
    benign_expectations: list[BenignExpectation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_requested_checks(self) -> HardeningCreate:
        if self.benign_attack_task_id and not self.benign_actions:
            raise ValueError("benign_actions are required with benign_attack_task_id")
        if bool(self.benign_actions) != bool(self.benign_expectations):
            raise ValueError("benign_actions and benign_expectations are required together")
        if self.benign_expectations and max(
            expectation.action_index for expectation in self.benign_expectations
        ) > len(self.benign_actions):
            raise ValueError("benign expectation action_index exceeds benign_actions")
        if not (
            self.include_exact_replay or self.include_nearby_bypass_search or self.benign_actions
        ):
            raise ValueError("at least one hardening check is required")
        return self


class ApiResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyVersionCreated(ApiResponse):
    policy_version_id: str
    version: int
    sha256: str


class PolicyVersionResponse(PolicyVersionCreated):
    policies: list[PolicyDocument]
    created_at: datetime


class CampaignResponse(ApiResponse):
    campaign_id: str
    target_version_id: str
    attack_task_id: str
    policy_version_id: str | None
    search_mode: str
    replay_episode_id: str | None
    run_kind: str
    source_finding_id: str | None
    source_episode_id: str | None
    red_config_id: str | None
    configuration: dict[str, Any]
    status: str
    cancellation_requested: bool
    episodes_started: int
    tokens_used: int
    cost_used: float
    containment_status: str
    containment_preflight: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class EpisodeSummaryResponse(ApiResponse):
    episode_id: str
    campaign_id: str
    status: str
    seed: int
    terminal_success: bool
    cumulative_reward: float
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    runtime_containment: dict[str, Any] | None


class EpisodeStepResponse(ApiResponse):
    step_id: str
    step_index: int
    red_action: dict[str, Any]
    public_observation: dict[str, Any]
    reward: float
    terminal_success: bool
    model_invocation_ids: list[str]


class EffectDetailResponse(ApiResponse):
    effect_id: str
    step_id: str | None
    attempt: dict[str, Any]
    decision: dict[str, Any] | None
    virtual_result: dict[str, Any] | None


class VerifierEventResponse(ApiResponse):
    verifier_event_id: str
    verifier_id: str
    signal: dict[str, Any]


class ModelInvocationAttributionResponse(ApiResponse):
    model_invocation_link_id: str
    episode_id: str
    step_id: str | None
    created_at: datetime


class ModelInvocationResponse(ApiResponse):
    model_invocation_id: str
    episode_id: str | None
    step_id: str | None
    provider: str
    model: str
    tokens: int
    cost: float
    latency_ms: int
    request_hash: str
    status: str
    role: str = "attacker"
    target_inference: dict[str, Any] | None = None
    attributions: list[ModelInvocationAttributionResponse]


class EpisodeDetailResponse(ApiResponse):
    episode_id: str
    campaign_id: str
    status: str
    seed: int
    terminal_success: bool
    cumulative_reward: float
    error: str | None
    runtime_containment: dict[str, Any] | None
    steps: list[EpisodeStepResponse]
    effects: list[EffectDetailResponse]
    verifier_events: list[VerifierEventResponse]
    model_invocations: list[ModelInvocationResponse]


class CampaignMetricsResponse(ApiResponse):
    campaign_id: str
    status: str
    episodes_started: int
    episodes_recorded: int
    successful_episodes: int
    step_count: int
    effect_count: int
    finding_count: int
    tokens_used: int
    cost_used: float


class FindingResponse(ApiResponse):
    finding_id: str
    campaign_id: str
    episode_id: str
    verifier_id: str
    severity: float
    status: str
    policy_version_id: str | None
    evidence_artifact_id: str | None
    created_at: datetime


class ReplayResponse(ApiResponse):
    source_finding_id: str
    replay_campaign_id: str
    mode: str
    policy_version_id: str | None
    hardening_run_id: str | None
    source_episode_id: str
    execution_contract: str


class HardeningLaunchResponse(ApiResponse):
    hardening_run_id: str
    status: str
    source_finding_id: str
    policy_version_id: str
    exact_replay_campaign_id: str | None
    nearby_bypass_campaign_id: str | None
    benign_regression_campaign_id: str | None


class HardeningRunResponse(ApiResponse):
    hardening_run_id: str
    finding_id: str
    policy_version_id: str
    status: str
    exact_replay_campaign_id: str | None
    nearby_bypass_campaign_id: str | None
    benign_regression_campaign_id: str | None
    result: dict[str, Any]
    artifact_id: str | None
    created_at: datetime
    completed_at: datetime | None


class ArtifactCreatedResponse(ApiResponse):
    artifact_id: str
    sha256: str
    size_bytes: int


class ArtifactMetadataResponse(ArtifactCreatedResponse):
    campaign_id: str | None
    episode_id: str | None
    kind: str
    metadata: dict[str, Any]
    parent_artifact_id: str | None
    created_at: datetime
    download_url: str


def get_repo(request: Request) -> Repository:
    return request.app.state.repository


def get_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store


def _campaign(row: Any, containment_preflight: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "campaign_id": row.id,
        "target_version_id": row.target_version_id,
        "attack_task_id": row.attack_task_id,
        "policy_version_id": row.policy_version_id,
        "search_mode": row.search_mode,
        "replay_episode_id": row.replay_episode_id,
        "run_kind": row.run_kind,
        "source_finding_id": row.source_finding_id,
        "source_episode_id": row.source_episode_id,
        "red_config_id": row.red_config_id,
        "configuration": row.configuration,
        "status": row.status,
        "cancellation_requested": row.cancellation_requested,
        "episodes_started": row.episodes_started,
        "tokens_used": row.tokens_used,
        "cost_used": row.cost_used,
        "containment_status": (
            containment_preflight["containment_status"]
            if containment_preflight is not None
            else "PENDING"
        ),
        "containment_preflight": containment_preflight,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
    }


def _target(row: Any) -> dict[str, Any]:
    return {"target_id": row.id, "name": row.name, "created_at": row.created_at}


def _target_version(row: Any, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    document = {
        "target_version_id": row.id,
        "target_id": row.target_id,
        "image": row.image,
        "image_digest": row.image_digest,
        "created_at": row.created_at,
    }
    if manifest is not None:
        document["manifest"] = manifest
    return document


def _task(row: Any) -> dict[str, Any]:
    return {
        "attack_task_id": row.id,
        "target_version_id": row.target_version_id,
        "document": row.document,
        "created_at": row.created_at,
    }


def _policy(row: Any, *, include_document: bool = True) -> dict[str, Any]:
    result = {
        "policy_version_id": row.id,
        "version": row.version,
        "sha256": row.content_hash,
        "created_at": row.created_at,
    }
    if include_document:
        result["policies"] = row.policies
    return result


def _artifact(row: Any) -> dict[str, Any]:
    return {
        "artifact_id": row.id,
        "campaign_id": row.campaign_id,
        "episode_id": row.episode_id,
        "kind": row.kind,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "metadata": row.metadata_json,
        "parent_artifact_id": row.parent_artifact_id,
        "created_at": row.created_at,
        "download_url": f"/v1/artifacts/{row.id}",
    }


def _slice(rows: list[Any], offset: int, limit: int) -> list[Any]:
    return rows[offset : offset + limit]


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    *,
    create_schema: bool | None = None,
    artifact_store: ArtifactStore | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    database = database or Database(settings.database_url)
    if create_schema is None:
        create_schema = settings.database_url.startswith("sqlite")
    if create_schema:
        database.create_all()
    telemetry = configure_telemetry(settings, service_name="control-api")
    app = FastAPI(title="Adversarial Agent MVP", version="0.1.0")
    app.state.database = database
    app.state.repository = Repository(database)
    app.state.telemetry = telemetry
    app.state.artifact_store = artifact_store or create_artifact_store(
        backend=settings.artifact_backend,
        local_root=settings.artifact_root,
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
    )
    from .research_api import install_research_api
    install_research_api(app, settings)
    from .guide_api import install_guide_api
    install_guide_api(app, settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins if cors_origins is None else cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-AML-API-Version"],
        expose_headers=["ETag", "X-Content-SHA256"],
    )
    if settings.otel_enabled:
        instrument_sqlalchemy(database.engine)
        instrument_fastapi(app)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness() -> dict[str, str]:
        try:
            with database.session() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ready"}

    @app.get("/v1/overview")
    def overview(repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        targets = repo.list_targets()
        versions = repo.list_target_versions()
        tasks = repo.list_attack_tasks()
        campaigns = repo.list_campaigns()
        episodes = repo.list_episodes()
        findings = repo.list_findings()
        policies = repo.list_policy_versions()
        artifacts = repo.list_artifacts()
        return {
            "counts": {
                "targets": len(targets),
                "target_versions": len(versions),
                "attack_tasks": len(tasks),
                "campaigns": len(campaigns),
                "episodes": len(episodes),
                "findings": len(findings),
                "policy_versions": len(policies),
                "artifacts": len(artifacts),
            },
            "campaign_statuses": {
                status: sum(campaign.status == status for campaign in campaigns)
                for status in sorted({str(campaign.status) for campaign in campaigns})
            },
            "total_tokens": sum(campaign.tokens_used for campaign in campaigns),
            "total_cost": sum(campaign.cost_used for campaign in campaigns),
            "verified_findings": sum(episode.terminal_success for episode in episodes),
        }

    @app.get("/v1/metrics")
    def metrics(
        campaign_id: str | None = None, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        if campaign_id:
            try:
                return repo.campaign_metrics(campaign_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        campaigns = repo.list_campaigns()
        return {
            "campaigns": len(campaigns),
            "episodes": len(repo.list_episodes()),
            "findings": len(repo.list_findings()),
            "tokens_used": sum(campaign.tokens_used for campaign in campaigns),
            "cost_used": sum(campaign.cost_used for campaign in campaigns),
        }

    @app.get("/v1/targets")
    def list_targets(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return [_target(row) for row in _slice(repo.list_targets(), offset, limit)]

    @app.get("/v1/scenarios")
    def list_scenarios(
        family: str | None = None,
        split: str | None = Query(default=None, pattern="^(train|validation|test|development)$"),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return ScenarioCatalog(repo).list_public(family=family, split=split, limit=limit, offset=offset)

    @app.get("/v1/scenarios/{scenario_version_id}")
    def get_scenario(scenario_version_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        try:
            return ScenarioCatalog(repo).get_public(scenario_version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="scenario version not found") from exc

    @app.post("/v1/targets", status_code=201)
    def create_target(
        payload: TargetCreate, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        try:
            row = repo.create_target(payload.name)
            return {"target_id": row.id, "name": row.name, "created_at": row.created_at}
        except Exception as exc:
            raise HTTPException(status_code=409, detail="target already exists") from exc

    @app.get("/v1/targets/{target_id}")
    def get_target(target_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        row = repo.get_target(target_id)
        if row is None:
            raise HTTPException(status_code=404, detail="target not found")
        document = _target(row)
        document["versions"] = [
            _target_version(version) for version in repo.list_target_versions(target_id=target_id)
        ]
        return document

    @app.get("/v1/target-versions")
    def list_target_versions(
        target_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        rows = _slice(repo.list_target_versions(target_id=target_id), offset, limit)
        return [_target_version(row) for row in rows]

    @app.post("/v1/target-versions", status_code=201)
    def create_target_version(
        payload: TargetVersionCreate, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        if payload.manifest.image != payload.manifest.image.strip():
            raise HTTPException(status_code=422, detail="invalid image reference")
        try:
            row = repo.create_target_version(
                payload.target_id,
                payload.manifest.image,
                payload.manifest.model_dump(mode="json"),
                payload.image_digest,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _target_version(row)

    @app.get("/v1/target-versions/{target_version_id}")
    def get_target_version(
        target_version_id: str, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        row = repo.get_target_version(target_version_id)
        if row is None:
            raise HTTPException(status_code=404, detail="target version not found")
        return _target_version(row, repo.load_manifest(row.id))

    @app.get("/v1/attack-tasks")
    def list_attack_tasks(
        target_version_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        rows = _slice(repo.list_attack_tasks(target_version_id=target_version_id), offset, limit)
        return [_task(row) for row in rows]

    @app.post("/v1/attack-tasks", status_code=201)
    def create_attack_task(
        payload: AttackTask, repo: Repository = Depends(get_repo)
    ) -> dict[str, str]:
        try:
            row = repo.create_attack_task(
                payload.task_id, payload.target_version_id, payload.model_dump(mode="json")
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"attack_task_id": row.id}

    @app.get("/v1/attack-tasks/{task_id}")
    def get_attack_task(task_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        row = repo.get_attack_task(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="attack task not found")
        return _task(row)

    @app.post(
        "/v1/policy-versions", status_code=201, response_model=PolicyVersionCreated
    )
    def create_policy_version(
        payload: PolicyVersionCreate, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        policies = [item.model_dump(mode="json") for item in payload.policies]
        row = repo.save_policy_version(policies)
        return {"policy_version_id": row.id, "version": row.version, "sha256": row.content_hash}

    @app.get("/v1/policy-versions", response_model=list[PolicyVersionResponse])
    def list_policy_versions(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return [_policy(row) for row in _slice(repo.list_policy_versions(), offset, limit)]

    @app.get(
        "/v1/policy-versions/{policy_version_id}", response_model=PolicyVersionResponse
    )
    def get_policy_version(
        policy_version_id: str, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        row = repo.get_policy_version(policy_version_id)
        if row is None:
            raise HTTPException(status_code=404, detail="policy version not found")
        return _policy(row)

    @app.post("/v1/red-experiment-configs", status_code=201)
    def create_red_experiment_config(
        payload: RedExperimentConfigCreate, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        try:
            document = RedExperimentConfig.model_validate(payload.document).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = repo.save_red_experiment_config(payload.name, document)
        return {
            "red_config_id": row.id,
            "name": row.name,
            "document": row.document,
            "sha256": row.content_hash,
            "created_at": row.created_at,
        }

    @app.get("/v1/red-experiment-configs")
    def list_red_experiment_configs(
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return [
            {
                "red_config_id": row.id,
                "name": row.name,
                "document": row.document,
                "sha256": row.content_hash,
                "created_at": row.created_at,
            }
            for row in repo.list_red_experiment_configs()
        ]

    @app.get("/v1/red-experiment-configs/{config_id}")
    def get_red_experiment_config(
        config_id: str, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        row = repo.get_red_experiment_config(config_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Red experiment configuration not found")
        return {
            "red_config_id": row.id,
            "name": row.name,
            "document": row.document,
            "sha256": row.content_hash,
            "created_at": row.created_at,
        }

    @app.get("/v1/campaigns", response_model=list[CampaignResponse])
    def list_campaigns(
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return [
            _campaign(row, repo.get_containment_preflight(row.id))
            for row in _slice(repo.list_campaigns(status=status), offset, limit)
        ]

    @app.post("/v1/campaigns", status_code=202, response_model=CampaignResponse)
    def create_campaign(
        payload: CampaignCreate, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        try:
            row = repo.create_campaign(
                payload.target_version_id,
                payload.attack_task_id,
                payload.search_mode,
                payload.policy_version_id,
                red_config_id=payload.red_config_id,
                configuration=payload.configuration,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _campaign(row, repo.get_containment_preflight(row.id))

    @app.get("/v1/campaigns/{campaign_id}", response_model=CampaignResponse)
    def get_campaign(campaign_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        row = repo.get_campaign(campaign_id)
        if row is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        return _campaign(row, repo.get_containment_preflight(row.id))

    @app.post("/v1/campaigns/{campaign_id}/cancel", status_code=202)
    def cancel_campaign(campaign_id: str, repo: Repository = Depends(get_repo)) -> dict[str, str]:
        try:
            repo.cancel_campaign(campaign_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        current = repo.get_campaign(campaign_id)
        if current is None:  # Defensive against external deletion after cancellation.
            raise HTTPException(status_code=404, detail="campaign not found")
        return {"campaign_id": campaign_id, "status": str(current.status)}

    @app.get(
        "/v1/campaigns/{campaign_id}/episodes",
        response_model=list[EpisodeSummaryResponse],
    )
    def list_campaign_episodes(
        campaign_id: str, repo: Repository = Depends(get_repo)
    ) -> list[dict[str, Any]]:
        if repo.get_campaign(campaign_id) is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        return [
            {
                "episode_id": episode.id,
                "campaign_id": episode.campaign_id,
                "status": episode.status,
                "seed": episode.seed,
                "terminal_success": episode.terminal_success,
                "cumulative_reward": episode.cumulative_reward,
                "error": episode.error,
                "created_at": episode.created_at,
                "started_at": episode.started_at,
                "completed_at": episode.completed_at,
                "runtime_containment": repo.get_runtime_containment(episode.id),
            }
            for episode in repo.list_episodes(campaign_id=campaign_id)
        ]

    @app.get(
        "/v1/campaigns/{campaign_id}/metrics", response_model=CampaignMetricsResponse
    )
    def campaign_metrics(campaign_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        try:
            return repo.campaign_metrics(campaign_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/episodes", response_model=list[EpisodeSummaryResponse])
    def list_episodes(
        campaign_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return [
            {
                "episode_id": episode.id,
                "campaign_id": episode.campaign_id,
                "status": episode.status,
                "seed": episode.seed,
                "terminal_success": episode.terminal_success,
                "cumulative_reward": episode.cumulative_reward,
                "error": episode.error,
                "created_at": episode.created_at,
                "started_at": episode.started_at,
                "completed_at": episode.completed_at,
                "runtime_containment": repo.get_runtime_containment(episode.id),
            }
            for episode in _slice(repo.list_episodes(campaign_id), offset, limit)
        ]

    @app.get("/v1/episodes/{episode_id}", response_model=EpisodeDetailResponse)
    def get_episode(episode_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        loaded = repo.get_episode(episode_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="episode not found")
        episode, steps = loaded
        effects = repo.list_effect_attempts(episode_id)
        decisions = repo.list_policy_decisions(episode_id)
        decision_by_effect = {decision.effect_id: decision for decision in decisions}
        invocations = repo.list_model_invocations(episode_id=episode_id)
        invocation_links = repo.list_model_invocation_links(episode_id=episode_id)
        links_by_invocation: dict[str, list[Any]] = {}
        invocation_ids_by_step: dict[str, list[str]] = {}
        for link in invocation_links:
            links_by_invocation.setdefault(link.model_invocation_id, []).append(link)
            if link.step_id is not None:
                invocation_ids_by_step.setdefault(link.step_id, []).append(link.model_invocation_id)
        return {
            "episode_id": episode.id,
            "campaign_id": episode.campaign_id,
            "status": episode.status,
            "seed": episode.seed,
            "terminal_success": episode.terminal_success,
            "cumulative_reward": episode.cumulative_reward,
            "error": episode.error,
            "runtime_containment": repo.get_runtime_containment(episode_id),
            "steps": [
                {
                    "step_id": step.id,
                    "step_index": step.step_index,
                    "red_action": step.red_action,
                    "public_observation": step.public_observation,
                    "reward": step.reward,
                    "terminal_success": step.terminal_success,
                    "model_invocation_ids": sorted(
                        {
                            *invocation_ids_by_step.get(step.id, []),
                            *(
                                [step.model_invocation_id]
                                if step.model_invocation_id is not None
                                else []
                            ),
                        }
                    ),
                }
                for step in steps
            ],
            "effects": [
                {
                    "effect_id": effect.id,
                    "step_id": effect.step_id,
                    "attempt": effect.document,
                    "decision": (
                        decision_by_effect[effect.id].document
                        if effect.id in decision_by_effect
                        else None
                    ),
                    "virtual_result": (
                        decision_by_effect[effect.id].result
                        if effect.id in decision_by_effect
                        else None
                    ),
                }
                for effect in effects
            ],
            "verifier_events": [
                {
                    "verifier_event_id": event.id,
                    "verifier_id": event.verifier_id,
                    "signal": event.document,
                }
                for event in repo.list_verifier_events(episode_id)
            ],
            "model_invocations": [
                {
                    "model_invocation_id": invocation.id,
                    "episode_id": invocation.episode_id,
                    "step_id": invocation.step_id,
                    "provider": invocation.provider,
                    "model": invocation.model,
                    "tokens": invocation.tokens,
                    "cost": invocation.cost,
                    "latency_ms": invocation.latency_ms,
                    "request_hash": invocation.request_hash,
                    "status": invocation.status,
                    "role": invocation.configuration.get("role", "attacker"),
                    "target_inference": invocation.configuration.get("target_inference"),
                    "attributions": [
                        {
                            "model_invocation_link_id": link.id,
                            "episode_id": link.episode_id,
                            "step_id": link.step_id,
                            "created_at": link.created_at,
                        }
                        for link in links_by_invocation.get(invocation.id, [])
                    ],
                }
                for invocation in invocations
            ],
        }

    @app.post(
        "/v1/episodes/{episode_id}/evidence",
        status_code=201,
        response_model=ArtifactCreatedResponse,
    )
    def create_evidence(
        episode_id: str,
        repo: Repository = Depends(get_repo),
        store: ArtifactStore = Depends(get_store),
    ) -> dict[str, Any]:
        loaded = repo.get_episode(episode_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="episode not found")
        try:
            artifact = EvidenceBuilder(repo, store).build_episode_bundle(
                loaded[0].campaign_id, episode_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "artifact_id": artifact.artifact_id,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }

    @app.get("/v1/artifacts", response_model=list[ArtifactMetadataResponse])
    def list_artifacts(
        campaign_id: str | None = None,
        episode_id: str | None = None,
        kind: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        rows = repo.list_artifacts(campaign_id=campaign_id, episode_id=episode_id, kind=kind)
        return [_artifact(row) for row in _slice(rows, offset, limit)]

    @app.get(
        "/v1/artifacts/{artifact_id}/metadata", response_model=ArtifactMetadataResponse
    )
    def artifact_metadata(artifact_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        row = repo.get_artifact(artifact_id)
        if row is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return _artifact(row)

    @app.get("/v1/artifacts/{artifact_id}")
    def download_artifact(
        artifact_id: str,
        repo: Repository = Depends(get_repo),
        store: ArtifactStore = Depends(get_store),
    ) -> Response:
        artifact = repo.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        data = store.get_bytes(artifact.uri)
        if not verify_artifact(data, artifact.sha256, artifact.size_bytes):
            raise HTTPException(status_code=500, detail="artifact integrity verification failed")
        return Response(
            data,
            media_type="application/json"
            if artifact.uri.endswith(".json")
            else "application/octet-stream",
            headers={"ETag": artifact.sha256, "X-Artifact-SHA256": artifact.sha256},
        )

    @app.get("/v1/strategies")
    def list_strategies(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return [
            {
                "strategy_id": row.id,
                "document": row.document,
                "created_at": row.created_at,
            }
            for row in _slice(repo.list_strategies(), offset, limit)
        ]

    @app.get("/v1/strategies/{strategy_id}")
    def get_strategy(strategy_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        row = repo.get_strategy(strategy_id)
        if row is None:
            raise HTTPException(status_code=404, detail="strategy not found")
        return {
            "strategy_id": row.id,
            "document": row.document,
            "created_at": row.created_at,
        }

    @app.get("/v1/operational-events")
    def list_operational_events(
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return [
            {
                "event_id": row.id,
                "aggregate_type": row.aggregate_type,
                "aggregate_id": row.aggregate_id,
                "event_type": row.event_type,
                "payload": row.payload,
                "created_at": row.created_at,
            }
            for row in repo.list_events(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                limit=limit,
                offset=offset,
            )
        ]

    @app.get("/v1/operational-events/{event_id}")
    def get_operational_event(
        event_id: str, repo: Repository = Depends(get_repo)
    ) -> dict[str, Any]:
        row = repo.get_event(event_id)
        if row is None:
            raise HTTPException(status_code=404, detail="operational event not found")
        return {
            "event_id": row.id,
            "aggregate_type": row.aggregate_type,
            "aggregate_id": row.aggregate_id,
            "event_type": row.event_type,
            "payload": row.payload,
            "created_at": row.created_at,
        }

    @app.get("/v1/findings", response_model=list[FindingResponse])
    def list_findings(
        campaign_id: str | None = None,
        episode_id: str | None = None,
        status: str | None = None,
        repo: Repository = Depends(get_repo),
    ) -> list[dict[str, Any]]:
        return [
            {
                "finding_id": row.id,
                "campaign_id": row.campaign_id,
                "episode_id": row.episode_id,
                "verifier_id": row.verifier_id,
                "severity": row.severity,
                "status": row.status,
                "policy_version_id": row.policy_version_id,
                "evidence_artifact_id": row.evidence_artifact_id,
                "created_at": row.created_at,
            }
            for row in repo.list_findings(
                campaign_id=campaign_id, episode_id=episode_id, status=status
            )
        ]

    @app.get("/v1/findings/{finding_id}", response_model=FindingResponse)
    def get_finding(finding_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        row = repo.get_finding(finding_id)
        if row is None:
            raise HTTPException(status_code=404, detail="finding not found")
        return {
            "finding_id": row.id,
            "campaign_id": row.campaign_id,
            "episode_id": row.episode_id,
            "verifier_id": row.verifier_id,
            "severity": row.severity,
            "status": row.status,
            "policy_version_id": row.policy_version_id,
            "evidence_artifact_id": row.evidence_artifact_id,
            "created_at": row.created_at,
        }

    @app.post(
        "/v1/findings/{finding_id}/replay", status_code=202, response_model=ReplayResponse
    )
    def replay_finding(
        finding_id: str,
        payload: ReplayCreate,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        finding = repo.get_finding(finding_id)
        if finding is None:
            raise HTTPException(status_code=404, detail="finding not found")
        original = repo.get_campaign(finding.campaign_id)
        if original is None:
            raise HTTPException(status_code=409, detail="source campaign missing")
        replay_policy_id = (
            original.policy_version_id if payload.reproduction_only else payload.policy_version_id
        )
        if (
            payload.reproduction_only
            and payload.policy_version_id is not None
            and payload.policy_version_id != replay_policy_id
        ):
            raise HTTPException(
                status_code=422, detail="reproduction must preserve source campaign conditions"
            )
        try:
            if replay_policy_id and not payload.reproduction_only:
                repo.associate_finding_policy(finding_id, replay_policy_id)
            replay = repo.create_campaign(
                original.target_version_id,
                original.attack_task_id,
                "adaptive" if payload.search_nearby_bypasses else "linear",
                replay_policy_id,
                None if payload.search_nearby_bypasses else finding.episode_id,
                run_kind=("NEARBY_BYPASS" if payload.search_nearby_bypasses else "EXACT_REPLAY"),
                source_finding_id=finding_id,
                source_episode_id=finding.episode_id,
                red_config_id=original.red_config_id,
                configuration={"requires_source_seeded_mutation": payload.search_nearby_bypasses},
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        hardening_run_id = None
        if replay_policy_id and not payload.reproduction_only:
            hardening = repo.create_hardening_run(
                finding_id,
                replay_policy_id,
                exact_replay_campaign_id=(None if payload.search_nearby_bypasses else replay.id),
                bypass_campaign_id=(replay.id if payload.search_nearby_bypasses else None),
            )
            hardening_run_id = hardening.id
        return {
            "source_finding_id": finding_id,
            "replay_campaign_id": replay.id,
            "mode": "nearby_bypass_search" if payload.search_nearby_bypasses else "exact_replay",
            "policy_version_id": replay_policy_id,
            "hardening_run_id": hardening_run_id,
            "source_episode_id": finding.episode_id,
            "execution_contract": (
                "source_seeded_mutation"
                if payload.search_nearby_bypasses
                else "immutable_exact_action_replay"
            ),
        }

    @app.post(
        "/v1/findings/{finding_id}/hardening",
        status_code=202,
        response_model=HardeningLaunchResponse,
    )
    def create_hardening(
        finding_id: str,
        payload: HardeningCreate,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        finding = repo.get_finding(finding_id)
        if finding is None:
            raise HTTPException(status_code=404, detail="finding not found")
        original = repo.get_campaign(finding.campaign_id)
        if original is None:
            raise HTTPException(status_code=409, detail="source campaign missing")
        try:
            repo.associate_finding_policy(finding_id, payload.policy_version_id)
            exact = (
                repo.create_campaign(
                    original.target_version_id,
                    original.attack_task_id,
                    "linear",
                    payload.policy_version_id,
                    finding.episode_id,
                    run_kind="EXACT_REPLAY",
                    source_finding_id=finding_id,
                    source_episode_id=finding.episode_id,
                    red_config_id=original.red_config_id,
                )
                if payload.include_exact_replay
                else None
            )
            bypass = (
                repo.create_campaign(
                    original.target_version_id,
                    original.attack_task_id,
                    "adaptive",
                    payload.policy_version_id,
                    run_kind="NEARBY_BYPASS",
                    source_finding_id=finding_id,
                    source_episode_id=finding.episode_id,
                    red_config_id=original.red_config_id,
                    configuration={"requires_source_seeded_mutation": True},
                )
                if payload.include_nearby_bypass_search
                else None
            )
            benign = None
            if payload.benign_actions:
                benign_task_id = payload.benign_attack_task_id or original.attack_task_id
                benign_task = repo.get_attack_task(benign_task_id)
                if benign_task is None:
                    raise KeyError("benign attack task not found")
                if benign_task.target_version_id != original.target_version_id:
                    raise ValueError("benign task target version is inconsistent")
                benign = repo.create_campaign(
                    original.target_version_id,
                    benign_task.id,
                    "linear",
                    payload.policy_version_id,
                    run_kind="BENIGN_REGRESSION",
                    source_finding_id=finding_id,
                    source_episode_id=finding.episode_id,
                    red_config_id=original.red_config_id,
                    configuration={
                        "benign_actions": [
                            action.model_dump(mode="json") for action in payload.benign_actions
                        ],
                        "benign_expectations": [
                            expectation.model_dump(mode="json")
                            for expectation in payload.benign_expectations
                        ],
                    },
                )
            run = repo.create_hardening_run(
                finding_id,
                payload.policy_version_id,
                exact_replay_campaign_id=exact and exact.id,
                bypass_campaign_id=bypass and bypass.id,
                benign_campaign_id=benign and benign.id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "hardening_run_id": run.id,
            "status": run.status,
            "source_finding_id": finding_id,
            "policy_version_id": payload.policy_version_id,
            "exact_replay_campaign_id": run.exact_replay_campaign_id,
            "nearby_bypass_campaign_id": run.bypass_campaign_id,
            "benign_regression_campaign_id": run.benign_campaign_id,
        }

    @app.get("/v1/hardening-runs", response_model=list[HardeningRunResponse])
    def list_hardening_runs(
        finding_id: str | None = None, repo: Repository = Depends(get_repo)
    ) -> list[dict[str, Any]]:
        return [
            {
                "hardening_run_id": row.id,
                "finding_id": row.finding_id,
                "policy_version_id": row.policy_version_id,
                "status": row.status,
                "exact_replay_campaign_id": row.exact_replay_campaign_id,
                "nearby_bypass_campaign_id": row.bypass_campaign_id,
                "benign_regression_campaign_id": row.benign_campaign_id,
                "result": row.result,
                "artifact_id": row.artifact_id,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
            }
            for row in repo.list_hardening_runs(finding_id)
        ]

    @app.get("/v1/hardening-runs/{run_id}", response_model=HardeningRunResponse)
    def get_hardening_run(run_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        row = repo.get_hardening_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="hardening run not found")
        return {
            "hardening_run_id": row.id,
            "finding_id": row.finding_id,
            "policy_version_id": row.policy_version_id,
            "status": row.status,
            "exact_replay_campaign_id": row.exact_replay_campaign_id,
            "nearby_bypass_campaign_id": row.bypass_campaign_id,
            "benign_regression_campaign_id": row.benign_campaign_id,
            "result": row.result,
            "artifact_id": row.artifact_id,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }

    @app.post(
        "/v1/hardening-runs/{run_id}/evidence",
        status_code=201,
        response_model=ArtifactCreatedResponse,
    )
    def create_hardening_evidence(
        run_id: str,
        repo: Repository = Depends(get_repo),
        store: ArtifactStore = Depends(get_store),
    ) -> dict[str, Any]:
        run = repo.get_hardening_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="hardening run not found")
        if run.status not in {"PASSED", "FAILED", "BYPASS_FOUND"}:
            raise HTTPException(
                status_code=409,
                detail="hardening evidence requires a completed hardening run",
            )
        artifact = EvidenceBuilder(repo, store).build_hardening_bundle(run_id)
        return {
            "artifact_id": artifact.artifact_id,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }

    return app

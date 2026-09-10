from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, text

from .research import (
    ResearchError,
    ResearchService,
    command_view,
    lock_owner,
    owned_record,
    owned_session,
    record_view,
    session_view,
)
from .research_auth import ResearchAuthMiddleware, principal
from .research_contracts import (
    API_VERSION,
    CheckpointCreate,
    GenerationCreate,
    ResearchRunCreate,
    ResetRequest,
    RewardAnnotation,
    RunEvent,
    RuntimeCreate,
    SessionCreate,
    StepRequest,
    SuiteCreate,
)
from .research_storage import EpisodeCommand, ResearchRecord, ResearchSession
from .runtime_registry import RegisteredAttacker
from .scenarios import content_hash
from .settings import Settings
from .storage import (
    Artifact,
    Campaign,
    Episode,
    Repository,
    ScenarioVersionRow,
    TargetBundleRow,
    WorkLease,
    utc_iso,
)


def request_key(value: str | None) -> str:
    if value is None or not 1 <= len(value) <= 200 or not value.isascii():
        raise ResearchError("an ASCII Idempotency-Key of 1–200 characters is required", 422)
    return value


def owned_artifact(db: Any, owner_id: str, artifact_id: str) -> Artifact:
    row = db.get(Artifact, artifact_id)
    if row is None or row.metadata_json.get("owner_id") != owner_id:
        raise ResearchError("artifact not found", 404)
    return row


def install_research_api(app: FastAPI, settings: Settings) -> None:
    repository: Repository = app.state.repository
    service = ResearchService(repository, settings)
    app.state.research_service = service
    app.add_middleware(ResearchAuthMiddleware, settings=settings)

    @app.exception_handler(ResearchError)
    async def research_error(_request: Request, exc: ResearchError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "api_version": API_VERSION}, status_code=exc.status)

    router = APIRouter(prefix="/v1")

    @router.get("/research-catalog")
    def catalog(request: Request) -> dict[str, Any]:
        identity = principal(request)
        with repository.db.session() as db:
            rows = []
            recent = list(db.scalars(select(ResearchSession).where(
                ResearchSession.owner_id == identity["owner_id"]).order_by(ResearchSession.created_at.desc())))
            for bundle in db.scalars(select(TargetBundleRow).order_by(TargetBundleRow.created_at)):
                scenario = db.get(ScenarioVersionRow, bundle.scenario_version_id)
                assert scenario
                if scenario.split == "test" and not {"operator", "evaluation"} & set(identity["scopes"]):
                    continue
                last = next((s for s in recent if s.document["bundle_id"] == bundle.id), None)
                rows.append({"bundle_id": bundle.id, "target_version_id": bundle.target_version_id,
                    "scenario_version_id": bundle.scenario_version_id, "execution_mode": bundle.execution_mode,
                    "scenario": scenario.public_document, "readiness": "registered",
                    "live_acceptance": "not_asserted", "last_session": {
                        "id": last.id, "state": last.state, "stop_reason": last.stop_reason,
                        "created_at": utc_iso(last.created_at)} if last else None})
            return {"api_version": API_VERSION, "items": rows}

    @router.post("/research-sessions", status_code=202)
    def create_session(body: SessionCreate, request: Request,
                       idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        identity = principal(request)
        return service.create_session(str(identity["owner_id"]), request_key(idempotency_key), body,
            allow_test=bool({"operator", "evaluation"} & set(identity["scopes"])))

    @router.get("/research-sessions")
    def list_sessions(request: Request, limit: int = Query(default=100, ge=1, le=500),
                      offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
        owner = principal(request)["owner_id"]
        with repository.db.session() as db:
            return {"items": [session_view(row) for row in db.scalars(select(ResearchSession).where(
                ResearchSession.owner_id == owner).order_by(ResearchSession.created_at.desc()).offset(offset).limit(limit))]}

    @router.get("/research-sessions/{session_id}")
    def get_session(session_id: str, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            row = owned_session(db, principal(request)["owner_id"], session_id)
            result = session_view(row)
            campaign = db.get(Campaign, row.campaign_id)
            result["usage"] = {"tokens": campaign.tokens_used, "cost": campaign.cost_used} if campaign else {}
            return result

    @router.post("/research-sessions/{session_id}/heartbeat")
    def heartbeat(session_id: str, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            owner = principal(request)["owner_id"]
            lock_owner(db, owner)
            row = owned_session(db, owner, session_id)
            row.heartbeat_at = datetime.now(UTC)
            return session_view(row)

    @router.post("/research-sessions/{session_id}/reset", status_code=202)
    def reset(session_id: str, body: ResetRequest, request: Request,
              idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        return service.submit(principal(request)["owner_id"], session_id, "reset", request_key(idempotency_key), body.model_dump(mode="json"))

    @router.post("/research-sessions/{session_id}/steps", status_code=202)
    def step(session_id: str, body: StepRequest, request: Request,
             idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        key = request_key(idempotency_key)
        if "action_id" not in body.action.model_fields_set:
            body.action.action_id = "action_" + content_hash({"session": session_id, "key": key})[:32]
        return service.submit(principal(request)["owner_id"], session_id, "step", key, body.model_dump(mode="json"))

    @router.post("/research-sessions/{session_id}/close", status_code=202)
    def close(session_id: str, request: Request,
              idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        return service.submit(principal(request)["owner_id"], session_id, "close", request_key(idempotency_key), {})

    @router.post("/research-sessions/{session_id}/cancel", status_code=202)
    def cancel(session_id: str, request: Request,
               idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        return service.submit(principal(request)["owner_id"], session_id, "cancel", request_key(idempotency_key), {})

    @router.get("/research-operations/{operation_id}")
    def operation(operation_id: str, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            row = db.get(EpisodeCommand, operation_id)
            if row is None:
                raise ResearchError("operation not found", 404)
            owned_session(db, principal(request)["owner_id"], row.session_id)
            return command_view(row)

    @router.get("/research-sessions/{session_id}/trajectory")
    def trajectory(session_id: str, request: Request, limit: int = Query(default=100, ge=1, le=500),
                   offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
        with repository.db.session() as db:
            owned_session(db, principal(request)["owner_id"], session_id)
            commands = db.scalars(select(EpisodeCommand).where(EpisodeCommand.session_id == session_id)
                .order_by(EpisodeCommand.created_at, EpisodeCommand.id).offset(offset).limit(limit))
            return {"items": [{**command_view(row), "input": row.payload} for row in commands]}

    @router.get("/research-episodes/{episode_id}/evidence")
    def episode_evidence(episode_id: str, request: Request) -> dict[str, Any]:
        owner = principal(request, "evidence")["owner_id"]
        with repository.db.session() as db:
            episode = db.get(Episode, episode_id)
            session = db.scalar(select(ResearchSession).where(ResearchSession.campaign_id == episode.campaign_id)) if episode else None
            if session is None or session.owner_id != owner:
                raise ResearchError("episode not found", 404)
        return {"effects": [r.document for r in repository.list_effect_attempts(episode_id)],
                "verifier_events": [r.document for r in repository.list_verifier_events(episode_id)]}

    @router.post("/research-runs", status_code=201)
    def create_run(body: ResearchRunCreate, request: Request,
                   idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        with repository.db.session() as db:
            owner = principal(request)["owner_id"]
            lock_owner(db, owner)
            for dataset in body.dataset_ids:
                owned_record(db, owner, dataset, "dataset")
                if not db.scalar(select(ResearchRecord).where(ResearchRecord.kind == "dataset_result", ResearchRecord.request_key == dataset)):
                    raise ResearchError("dataset is not complete")
            if body.parent_run_id:
                owned_record(db, owner, body.parent_run_id, "run")
            if body.resume_checkpoint_id:
                owned_record(db, owner, body.resume_checkpoint_id, "checkpoint")
            return record_view(service.put_record(db, owner, "run", request_key(idempotency_key),
                {**body.model_dump(mode="json"), "configuration_hash": content_hash(body.configuration)}))

    @router.post("/research-runs/{run_id}/events", status_code=201)
    def run_event(run_id: str, body: RunEvent, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            owner = principal(request)["owner_id"]
            lock_owner(db, owner)
            owned_record(db, owner, run_id, "run")
            for artifact in body.artifact_ids:
                owned_artifact(db, owner, artifact)
            return record_view(service.put_record(db, owner, "run_event", run_id + ":" + body.event_id,
                {**body.model_dump(mode="json"), "run_id": run_id, "usage_source": "externally_reported"}))

    @router.post("/research-runs/{run_id}/generations", status_code=201)
    def generation(run_id: str, body: GenerationCreate, request: Request) -> dict[str, Any]:
        if body.parsed_action and "action_id" not in body.parsed_action.model_fields_set:
            body.parsed_action.action_id = "action_" + content_hash({"run": run_id, "generation": body.generation_id})[:32]
        with repository.db.session() as db:
            owner = principal(request)["owner_id"]
            lock_owner(db, owner)
            owned_record(db, owner, run_id, "run")
            session = owned_session(db, owner, body.session_id)
            episode = db.get(Episode, body.episode_id)
            if session.run_id != run_id or not episode or episode.campaign_id != session.campaign_id:
                raise ResearchError("generation lineage does not match the run and episode")
            if body.checkpoint_id:
                owned_record(db, owner, body.checkpoint_id, "checkpoint")
            if body.runtime_id:
                runtime = db.get(ResearchRecord, body.runtime_id)
                if runtime is None or runtime.kind != "runtime" or runtime.document["checkpoint_id"] != body.checkpoint_id:
                    raise ResearchError("generation runtime/checkpoint mismatch")
            if body.parent_generation_id:
                owned_record(db, owner, body.parent_generation_id, "generation")
            for artifact in body.trainer_artifact_ids:
                owned_artifact(db, owner, artifact)
            return record_view(service.put_record(db, owner, "generation", run_id + ":" + body.generation_id,
                {**body.model_dump(mode="json"), "run_id": run_id, "source": "externally_reported"}))

    @router.post("/research-runs/{run_id}/rewards", status_code=201)
    def report_reward(run_id: str, body: RewardAnnotation, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            owner = principal(request)["owner_id"]
            lock_owner(db, owner)
            owned_record(db, owner, run_id, "run")
            command = db.get(EpisodeCommand, body.operation_id)
            if command is None:
                raise ResearchError("operation not found", 404)
            session = owned_session(db, owner, command.session_id)
            if session.run_id != run_id:
                raise ResearchError("operation is from another run")
            return record_view(service.put_record(db, owner, "reward_annotation", run_id + ":" + body.annotation_id,
                {**body.model_dump(mode="json"), "run_id": run_id, "authoritative": False}))

    @router.get("/research-runs")
    def runs(request: Request, limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
        with repository.db.session() as db:
            return {"items": [record_view(row) for row in db.scalars(select(ResearchRecord).where(
                ResearchRecord.owner_id == principal(request)["owner_id"], ResearchRecord.kind == "run")
                .order_by(ResearchRecord.created_at.desc()).offset(offset).limit(limit))]}

    @router.get("/research-runs/{run_id}")
    def run_detail(run_id: str, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            owner = principal(request)["owner_id"]
            result = record_view(owned_record(db, owner, run_id, "run"))
            events = list(db.scalars(select(ResearchRecord).where(ResearchRecord.owner_id == owner,
                ResearchRecord.kind == "run_event").order_by(ResearchRecord.created_at, ResearchRecord.id)))
            result["events"] = [record_view(row) for row in events if row.document["run_id"] == run_id]
            result["status"] = next((e["document"]["status"] for e in reversed(result["events"]) if e["document"]["status"]), "created")
            return result

    @router.post("/checkpoints", status_code=201)
    def checkpoint(body: CheckpointCreate, request: Request,
                   idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        with repository.db.session() as db:
            owner = principal(request)["owner_id"]
            lock_owner(db, owner)
            owned_record(db, owner, body.run_id, "run")
            files = []
            for entry in body.files:
                artifact = owned_artifact(db, owner, entry.artifact_id)
                files.append({**entry.model_dump(), "sha256": artifact.sha256, "size_bytes": artifact.size_bytes})
            return record_view(service.put_record(db, owner, "checkpoint", request_key(idempotency_key),
                {**body.model_dump(mode="json"), "files": files, "load_validation": "not_asserted"}))

    @router.post("/model-runtimes", status_code=201)
    def runtime(body: RuntimeCreate, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            owner = principal(request, "operator")["owner_id"]
            lock_owner(db, "__runtime_registry__")
            lock_owner(db, owner)
            checkpoint = db.get(ResearchRecord, body.checkpoint_id)
            if checkpoint is None or checkpoint.kind != "checkpoint":
                raise ResearchError("checkpoint not found", 404)
            # Names/versions are global operator-controlled identities.
            key = body.name + ":" + body.version
            existing = db.scalar(select(ResearchRecord).where(ResearchRecord.kind == "runtime", ResearchRecord.request_key == key))
            if existing:
                if existing.content_hash != content_hash(body.model_dump(mode="json")):
                    raise ResearchError("runtime versions are immutable")
                return record_view(existing)
            return record_view(service.put_record(db, owner, "runtime", key, body.model_dump(mode="json")))

    @router.post("/model-runtimes/{runtime_id}/health")
    async def runtime_health(runtime_id: str, request: Request) -> dict[str, Any]:
        owner = principal(request, "operator")["owner_id"]
        with repository.db.session() as db:
            runtime = db.get(ResearchRecord, runtime_id)
            if runtime is None or runtime.kind != "runtime":
                raise ResearchError("runtime not found", 404)
        model = RegisteredAttacker(runtime.document)
        try:
            health = {"status": "healthy", "identity": await model.health(), "runtime_id": runtime_id}
        except Exception:
            health = {"status": "unavailable", "runtime_id": runtime_id}
        finally:
            await model.close()
        health["checked_at"] = datetime.now(UTC).isoformat()
        with repository.db.session() as db:
            lock_owner(db, owner)
            service.put_record(db, owner, "runtime_health", runtime_id + ":" + health["checked_at"], health)
        return health

    @router.post("/benchmark-suites", status_code=201)
    def suite(body: SuiteCreate, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            owner = principal(request, "operator")["owner_id"]
            lock_owner(db, owner)
            cases = []
            for bundle_id in body.bundle_ids:
                bundle = db.get(TargetBundleRow, bundle_id)
                if bundle is None:
                    raise ResearchError("bundle not found", 404)
                scenario = db.get(ScenarioVersionRow, bundle.scenario_version_id)
                assert scenario
                cases.append({"bundle_id": bundle.id, "target_version_id": bundle.target_version_id,
                    "scenario_version_id": scenario.id, "split": scenario.split, "family": scenario.family,
                    "verifier_version": "deterministic-v1"})
            key = body.name + ":" + body.version
            return record_view(service.put_record(db, owner, "suite", key,
                {**body.model_dump(mode="json"), "cases": cases, "strategy_memory": "disabled"}))

    # Registry reads share ownership validation; immutable versions are the API identity.
    def registry_list(kind: str, request: Request) -> dict[str, Any]:
        identity = principal(request, "evaluation" if kind == "suite" else "research")
        with repository.db.session() as db:
            rows = list(db.scalars(select(ResearchRecord).where(ResearchRecord.kind == kind).order_by(ResearchRecord.created_at.desc()).limit(500)))
            if kind == "checkpoint":
                rows = [r for r in rows if r.owner_id == identity["owner_id"]]
            elif kind == "runtime" and "operator" not in identity["scopes"]:
                rows = [r for r in rows if (cp := db.get(ResearchRecord, r.document["checkpoint_id"])) and cp.owner_id == identity["owner_id"]]
            values = [record_view(row) for row in rows]
            if kind == "runtime":
                checks = list(db.scalars(select(ResearchRecord).where(ResearchRecord.kind == "runtime_health")
                    .order_by(ResearchRecord.created_at.desc())))
                for item in values:
                    last_check = next((c for c in checks if c.document["runtime_id"] == item["id"]), None)
                    item["health"] = last_check.document if last_check else {"status": "not_checked"}
            if kind == "runtime" and "operator" not in identity["scopes"]:
                for item in values:
                    item["document"] = {k: v for k, v in item["document"].items() if k not in {"endpoint", "credential_ref"}}
            return {"items": values}

    @router.get("/checkpoints")
    def checkpoints(request: Request) -> dict[str, Any]:
        return registry_list("checkpoint", request)

    @router.get("/model-runtimes")
    def runtimes(request: Request) -> dict[str, Any]:
        return registry_list("runtime", request)

    @router.get("/benchmark-suites")
    def suites(request: Request) -> dict[str, Any]:
        return registry_list("suite", request)

    @router.get("/checkpoints/{checkpoint_id}")
    def checkpoint_detail(checkpoint_id: str, request: Request) -> dict[str, Any]:
        with repository.db.session() as db:
            return record_view(owned_record(db, principal(request)["owner_id"], checkpoint_id, "checkpoint"))

    @router.get("/research-artifacts/{artifact_id}/download")
    def artifact_download(artifact_id: str, request: Request) -> StreamingResponse:
        with repository.db.session() as db:
            row = owned_artifact(db, principal(request)["owner_id"], artifact_id)
        return StreamingResponse(app.state.artifact_store.iter_bytes(row.uri), media_type="application/octet-stream",
            headers={"Content-Length": str(row.size_bytes), "ETag": f'"{row.sha256}"',
                     "X-Content-SHA256": row.sha256})

    @router.get("/research-health")
    async def health(request: Request) -> dict[str, Any]:
        principal(request)
        components: dict[str, Any] = {"api": "ready", "database": "ready", "supervisor": "not_configured",
            "target": "per_session", "model_endpoint": "per_runtime", "artifacts": "unverified"}
        with repository.db.session() as db:
            db.execute(text("SELECT 1"))
            queued = len(list(db.scalars(select(WorkLease.id).where(WorkLease.status == "PENDING"))))
        if settings.capsule_supervisor_url:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    response = await client.get(settings.capsule_supervisor_url.rstrip("/") + "/healthz")
                    components["supervisor"] = "ready" if response.is_success else "unavailable"
            except httpx.HTTPError:
                components["supervisor"] = "unavailable"
        store = app.state.artifact_store
        try:
            if settings.artifact_backend == "local":
                import os
                components["artifacts"] = "ready" if os.access(store.root, os.W_OK) else "unavailable"
            else:
                store.client.head_bucket(Bucket=store.bucket)
                components["artifacts"] = "ready"
        except Exception:
            components["artifacts"] = "unavailable"
        return {"components": components, "queue_depth": queued,
            "capacity": {"workers": settings.max_worker_concurrency, "capsules": settings.max_capsule_concurrency,
                "sessions_per_owner": settings.research_max_sessions_per_owner}}

    app.include_router(router)
    from .research_evaluations import install_evaluation_routes
    from .research_transfers import install_transfer_routes
    install_transfer_routes(app, service)
    install_evaluation_routes(app, service)

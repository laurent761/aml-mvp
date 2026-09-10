from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Request
from sqlalchemy import select

from .contracts import new_id
from .evaluation import (
    EvaluationConfig,
    EvaluationSample,
    EvaluationSplit,
    FailureCategory,
    run_evaluation,
)
from .red_contracts import AblationMode
from .research import (
    TERMINAL_SESSIONS,
    ResearchError,
    ResearchService,
    lock_owner,
    owned_record,
    record_view,
)
from .research_api import request_key
from .research_auth import principal
from .research_contracts import EvaluationCreate, SessionCreate, SessionLimits
from .research_storage import EpisodeCommand, ResearchRecord, ResearchSession
from .storage import Artifact, Campaign, Episode, WorkLease, jsonable


def install_evaluation_routes(app: FastAPI, service: ResearchService) -> None:
    router = APIRouter(prefix="/v1")
    repo = service.repository

    @router.post("/evaluations", status_code=202)
    def evaluation(body: EvaluationCreate, request: Request,
                   idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        with repo.db.session() as db:
            owner = principal(request, "evaluation")["owner_id"]
            lock_owner(db, owner)
            suite = db.get(ResearchRecord, body.suite_id)
            if suite is None or suite.kind != "suite":
                raise ResearchError("benchmark suite not found", 404)
            for role in ("baseline", "candidate"):
                checkpoint_id = getattr(body, role + "_checkpoint_id")
                owned_record(db, owner, checkpoint_id, "checkpoint")
                runtime_id = getattr(body, role + "_runtime_id")
                if runtime_id:
                    runtime = db.get(ResearchRecord, runtime_id)
                    if runtime is None or runtime.kind != "runtime" or runtime.document["checkpoint_id"] != checkpoint_id:
                        raise ResearchError("approved runtime does not match the checkpoint")
            key = request_key(idempotency_key)
            prior = db.scalar(select(ResearchRecord).where(ResearchRecord.owner_id == owner,
                ResearchRecord.kind == "evaluation", ResearchRecord.request_key == key))
            row = service.put_record(db, owner, "evaluation", key,
                {**body.model_dump(mode="json"), "suite_hash": suite.content_hash,
                 "strategy_memory": "disabled", "configuration_version": "aml.evaluation.v1"})
            if prior is None:
                db.add(WorkLease(id=new_id("job"), job_type="research_evaluation", payload={"evaluation_id": row.id}))
            return record_view(row)

    @router.get("/evaluations")
    def evaluations(request: Request) -> dict[str, Any]:
        with repo.db.session() as db:
            return {"items": [record_view(row) for row in db.scalars(select(ResearchRecord).where(
                ResearchRecord.owner_id == principal(request, "evaluation")["owner_id"], ResearchRecord.kind == "evaluation")
                .order_by(ResearchRecord.created_at.desc()).limit(500))]}

    @router.get("/evaluations/{evaluation_id}")
    def evaluation_detail(evaluation_id: str, request: Request) -> dict[str, Any]:
        with repo.db.session() as db:
            owner = principal(request, "evaluation")["owner_id"]
            result = record_view(owned_record(db, owner, evaluation_id, "evaluation"))
            complete = db.scalar(select(ResearchRecord).where(ResearchRecord.kind == "evaluation_result",
                ResearchRecord.request_key == evaluation_id))
            result["status"] = "completed" if complete else "running"
            result["report"] = complete.document if complete else None
            result["cases"] = [record_view(r) for r in db.scalars(select(ResearchRecord).where(
                ResearchRecord.kind == "evaluation_case", ResearchRecord.owner_id == owner).order_by(ResearchRecord.created_at))
                if r.document["evaluation_id"] == evaluation_id]
            result["sessions"] = [{"id": row.id, "state": row.state, "case": row.document["evaluation"]}
                for row in db.scalars(select(ResearchSession).where(ResearchSession.owner_id == owner))
                if (row.document.get("evaluation") or {}).get("evaluation_id") == evaluation_id]
            if not complete:
                jobs = [j for j in db.scalars(select(WorkLease).where(WorkLease.job_type == "research_evaluation")) if j.payload["evaluation_id"] == evaluation_id]
                if jobs and all(j.status == "FAILED" for j in jobs):
                    result["status"] = "failed"
            return result

    @router.post("/research-episodes/{episode_id}/reproduce", status_code=202)
    def reproduce(episode_id: str, request: Request,
                  idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        owner = principal(request, "evaluation")["owner_id"]
        with repo.db.session() as db:
            episode = db.get(Episode, episode_id)
            session = db.scalar(select(ResearchSession).where(ResearchSession.campaign_id == episode.campaign_id)) if episode else None
            if session is None or session.owner_id != owner or episode is None:
                raise ResearchError("episode not found", 404)
            if session.state not in TERMINAL_SESSIONS and session.episode_id == episode_id and session.state != "episode_done":
                raise ResearchError("source episode is still executing")
            commands = [c for c in db.scalars(select(EpisodeCommand).where(EpisodeCommand.session_id == session.id)
                .order_by(EpisodeCommand.created_at, EpisodeCommand.id)) if c.kind == "step" and c.status == "completed" and c.payload["episode_id"] == episode_id]
            if not commands:
                raise ResearchError("source has no completed actions to reproduce")
            body = SessionCreate(bundle_id=session.document["bundle_id"], run_id=session.run_id,
                group_id=session.document["group_id"], seed=episode.seed,
                policy_version_id=session.document["policy_version_id"],
                limits=SessionLimits.model_validate({**session.document["limits"], "max_episodes": 1}))
            source = {"reproduction": {"episode_id": episode_id,
                "actions": [c.payload["action"] for c in commands],
                "observations": [c.result["public_observation"] for c in commands if c.result],
                "source_terminal_success": episode.terminal_success}}
        return service.create_session(owner, "reproduce:" + request_key(idempotency_key), body,
                                      evaluation=source, allow_test=True)

    @router.get("/research-sessions/{session_id}/reproduction")
    def reproduction_result(session_id: str, request: Request) -> dict[str, Any]:
        with repo.db.session() as db:
            owner = principal(request, "evaluation")["owner_id"]
            session = db.get(ResearchSession, session_id)
            if session is None or session.owner_id != owner:
                raise ResearchError("session not found", 404)
            result = db.scalar(select(ResearchRecord).where(ResearchRecord.kind == "reproduction_result", ResearchRecord.request_key == session_id))
            return {"status": session.state, "result": result.document if result else None}

    app.include_router(router)


class EvaluationRunner:
    """Schedule bounded case pairs without occupying workers waiting for child jobs."""
    def __init__(self, service: ResearchService, store: Any):
        self.service, self.store = service, store

    async def run(self, evaluation_id: str) -> None:
        repo = self.service.repository
        with repo.db.session() as db:
            evaluation = db.get(ResearchRecord, evaluation_id)
            assert evaluation is not None
            suite = db.get(ResearchRecord, evaluation.document["suite_id"])
            assert suite is not None
            if db.scalar(select(ResearchRecord.id).where(ResearchRecord.kind == "evaluation_result", ResearchRecord.request_key == evaluation_id)):
                return
        all_samples: dict[str, list[EvaluationSample]] = {"baseline": [], "candidate": []}
        executed: list[dict[str, Any]] = []
        for case_index, case in enumerate(suite.document["cases"]):
            for seed in suite.document["seeds"]:
                pair_id = f"{case_index}:{seed}"
                pair: dict[str, Any] = {}
                for role in ("baseline", "candidate"):
                    body = SessionCreate(bundle_id=case["bundle_id"], mode=evaluation.document["mode"],
                        runtime_id=evaluation.document[role + "_runtime_id"], seed=seed,
                        group_id=evaluation_id, limits=SessionLimits.model_validate(suite.document["limits"]))
                    session = self.service.create_session(evaluation.owner_id, f"{evaluation_id}:{pair_id}:{role}", body,
                        evaluation={"evaluation_id": evaluation_id, "pair_id": pair_id, "role": role,
                            "checkpoint_id": evaluation.document[role + "_checkpoint_id"], "suite_hash": suite.content_hash}, allow_test=True)
                    pair[role] = session["id"]
                with repo.db.session() as db:
                    lock_owner(db, evaluation.owner_id)
                    self.service.put_record(db, evaluation.owner_id, "evaluation_case", f"{evaluation_id}:{pair_id}",
                        {"evaluation_id": evaluation_id, "pair_id": pair_id, "sessions": pair, "case": case, "seed": seed})
                    sessions = {role: db.get(ResearchSession, session_id) for role, session_id in pair.items()}
                    if any(row is None or row.state not in TERMINAL_SESSIONS for row in sessions.values()):
                        return
                    for role, row in sessions.items():
                        assert row is not None
                        campaign = db.get(Campaign, row.campaign_id)
                        assert campaign is not None
                        commands = list(db.scalars(select(EpisodeCommand).where(EpisodeCommand.session_id == row.id, EpisodeCommand.kind == "step").order_by(EpisodeCommand.created_at)))
                        steps = [c for c in commands if c.status == "completed" and c.result]
                        success = any(c.result and c.result["outcome"]["terminal_success"] for c in steps)
                        failed = row.state in {"interrupted", "failed", "expired", "cancelled"}
                        target_error = any(c.result and c.result["outcome"]["execution_status"] != "ok" for c in steps)
                        split = EvaluationSplit.DEVELOPMENT if case["split"] == "validation" else EvaluationSplit(case["split"])
                        sample = EvaluationSample(pair_id=pair_id, target_version_id=case["target_version_id"],
                            target_variant_id=case["scenario_version_id"], split=split, held_out=split == EvaluationSplit.TEST,
                            success=bool(success), total_cost=campaign.cost_used,
                            verified_findings=int(bool(success)), steps_to_success=next((i + 1 for i, c in enumerate(steps) if c.result and c.result["outcome"]["terminal_success"]), None),
                            failure_categories=(FailureCategory.TARGET_ERROR,) if failed or target_error else ())
                        all_samples[role].append(sample)
                        executed.append({"role": role, "session_id": row.id, "episode_id": row.episode_id,
                            "operation_ids": [c.id for c in commands], "sample": asdict(sample),
                            "infrastructure_failure": failed, "stop_reason": row.stop_reason,
                            "tokens": campaign.tokens_used, "cost": campaign.cost_used})
        config = EvaluationConfig(evaluation_id=evaluation_id, red_version=evaluation.document["candidate_checkpoint_id"],
            benchmark_version=suite.document["version"], dataset_version=suite.content_hash,
            ablation=AblationMode.MODEL_ONLY, random_seed=suite.document["seeds"][0])
        report = jsonable(asdict(run_evaluation(config, all_samples["candidate"], baseline_samples=all_samples["baseline"])))
        document = {"report": report, "executed_cases": jsonable(executed),
            "suite_id": suite.id, "suite_hash": suite.content_hash,
            "baseline_checkpoint_id": evaluation.document["baseline_checkpoint_id"],
            "candidate_checkpoint_id": evaluation.document["candidate_checkpoint_id"],
            "reproduction": "separate_fresh_episode_measurement", "deterministic_replay_guaranteed": False}
        stored = self.store.put_json("evaluations", document)
        with repo.db.session() as db:
            owner = lock_owner(db, evaluation.owner_id)
            if db.scalar(select(ResearchRecord.id).where(ResearchRecord.kind == "evaluation_result", ResearchRecord.request_key == evaluation_id)):
                return
            if owner.reserved_bytes + stored.size_bytes > self.service.settings.research_owner_storage_bytes:
                raise ResearchError("evaluation report exceeds storage quota")
            owner.reserved_bytes += stored.size_bytes
            db.add(Artifact(id=stored.artifact_id, kind="evaluation", uri=stored.uri, sha256=stored.sha256,
                size_bytes=stored.size_bytes, metadata_json={"owner_id": evaluation.owner_id, "evaluation_id": evaluation_id}))
            db.flush()
            self.service.put_record(db, evaluation.owner_id, "evaluation_result", evaluation_id,
                {**document, "artifact_id": stored.artifact_id, "sha256": stored.sha256})

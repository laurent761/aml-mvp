from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .contracts import CampaignStatus, new_id
from .research_contracts import API_VERSION, SessionCreate
from .research_storage import EpisodeCommand, ResearchOwner, ResearchRecord, ResearchSession
from .scenarios import content_hash
from .settings import Settings
from .storage import (
    Campaign,
    Repository,
    ScenarioVersionRow,
    TargetBundleRow,
    WorkLease,
    jsonable,
    utc_iso,
)

# The POC has one Admin. Keep the existing local owner key for stored records.
ADMIN_OWNER_ID = "local"

TERMINAL_SESSIONS = {"closed", "cancelled", "expired", "interrupted", "failed", "completed"}


class ResearchError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def lock_owner(db: Session, owner_id: str) -> ResearchOwner:
    """A database lock, including SQLite, serializes quota reservations and idempotency."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(ResearchOwner).values(id=owner_id).on_conflict_do_nothing())
    db.execute(update(ResearchOwner).where(ResearchOwner.id == owner_id).values(
        revision=ResearchOwner.revision + 1
    ))
    return db.scalars(select(ResearchOwner).where(ResearchOwner.id == owner_id)
        .execution_options(populate_existing=True)).one()


def owned_record(db: Session, owner_id: str, record_id: str, kind: str | None = None) -> ResearchRecord:
    row = db.get(ResearchRecord, record_id)
    if row is None or row.owner_id != owner_id or (kind and row.kind != kind):
        raise ResearchError("record not found", 404)
    return row


def owned_session(db: Session, owner_id: str, session_id: str) -> ResearchSession:
    row = db.get(ResearchSession, session_id)
    if row is None or row.owner_id != owner_id:
        raise ResearchError("session not found", 404)
    return row


def record_view(row: ResearchRecord) -> dict[str, Any]:
    return {"api_version": API_VERSION, "id": row.id, "kind": row.kind,
            "content_hash": row.content_hash, "created_at": utc_iso(row.created_at),
            "document": row.document}


def session_view(row: ResearchSession) -> dict[str, Any]:
    document = {**row.document}
    if document.get("runtime"):
        document["runtime"] = {k: v for k, v in document["runtime"].items() if k not in {"endpoint", "credential_ref"}}
    evaluation = document.get("evaluation")
    if evaluation and evaluation.get("reproduction"):
        document["evaluation"] = {"reproduction": {"episode_id": evaluation["reproduction"]["episode_id"]}}
    return {"api_version": API_VERSION, "id": row.id, "owner_id": row.owner_id,
            "campaign_id": row.campaign_id, "run_id": row.run_id, "state": row.state,
            "episode_id": row.episode_id, "step_index": row.step_index,
            "stop_reason": row.stop_reason, "configuration": document,
            "created_at": utc_iso(row.created_at), "heartbeat_at": utc_iso(row.heartbeat_at)}


def command_view(row: EpisodeCommand) -> dict[str, Any]:
    return {"api_version": API_VERSION, "id": row.id, "session_id": row.session_id,
            "kind": row.kind, "status": row.status, "result": row.result,
            "created_at": utc_iso(row.created_at),
            "completed_at": utc_iso(row.completed_at) if row.completed_at else None}


class ResearchService:
    def __init__(self, repository: Repository, settings: Settings):
        self.repository, self.settings = repository, settings

    @staticmethod
    def put_record(db: Session, owner_id: str, kind: str, key: str, document: dict[str, Any]) -> ResearchRecord:
        digest = content_hash(document)
        prior = db.scalar(select(ResearchRecord).where(
            ResearchRecord.owner_id == owner_id, ResearchRecord.kind == kind,
            ResearchRecord.request_key == key,
        ))
        if prior:
            if prior.content_hash != digest:
                raise ResearchError("idempotency key was reused with different content")
            return prior
        row = ResearchRecord(id=new_id(kind), owner_id=owner_id, kind=kind,
                             request_key=key, content_hash=digest, document=jsonable(document))
        db.add(row)
        db.flush()
        return row

    def create_session(self, owner_id: str, key: str, request: SessionCreate, *,
                       evaluation: dict[str, Any] | None = None) -> dict[str, Any]:
        config_id = self.repository.default_red_experiment_config().id
        with self.repository.db.session() as db:
            owner = lock_owner(db, owner_id)
            fingerprint = content_hash({"request": request.model_dump(mode="json"), "evaluation": evaluation})
            prior = db.scalar(select(ResearchSession).where(
                ResearchSession.owner_id == owner_id, ResearchSession.request_key == key))
            if prior:
                if prior.request_hash != fingerprint:
                    raise ResearchError("idempotency key was reused with different content")
                return session_view(prior)
            active = list(db.scalars(select(ResearchSession.id).where(
                ResearchSession.owner_id == owner_id, ResearchSession.state.not_in(TERMINAL_SESSIONS))))
            if len(active) >= self.settings.research_max_sessions_per_owner:
                raise ResearchError("owner concurrent session quota exhausted", 429)
            if owner.spent_cost + owner.reserved_cost + request.limits.max_cost > self.settings.research_owner_cost_limit:
                raise ResearchError("owner cost quota exhausted", 429)
            bundle = db.get(TargetBundleRow, request.bundle_id)
            if bundle is None:
                raise ResearchError("bundle not found", 404)
            scenario = db.get(ScenarioVersionRow, bundle.scenario_version_id)
            assert scenario is not None
            if request.run_id:
                owned_record(db, owner_id, request.run_id, "run")
            runtime_document = None
            if request.runtime_id:
                runtime = db.get(ResearchRecord, request.runtime_id)
                if runtime is None or runtime.kind != "runtime":
                    raise ResearchError("approved runtime not found", 404)
                runtime_document = runtime.document
                owned_record(db, owner_id, runtime.document["checkpoint_id"], "checkpoint")
            if request.policy_version_id:
                from .storage import PolicyVersion
                if db.get(PolicyVersion, request.policy_version_id) is None:
                    raise ResearchError("policy version not found", 404)
            document = {**request.model_dump(mode="json"),
                "scenario_version_id": scenario.id, "target_version_id": bundle.target_version_id,
                "attack_task_id": bundle.attack_task_id, "verifier_version": "deterministic-v1",
                "baseline_reward_version": "aml.baseline.v1", "split": scenario.split,
                "family": scenario.family, "runtime": runtime_document,
                "evaluation": evaluation, "strategy_memory": "disabled"}
            campaign = Campaign(id=new_id("campaign"), target_version_id=bundle.target_version_id,
                attack_task_id=bundle.attack_task_id, policy_version_id=request.policy_version_id,
                search_mode="linear", run_kind="RESEARCH", red_config_id=config_id,
                configuration={"research": document}, status=CampaignStatus.CREATED)
            db.add(campaign)
            db.flush()
            session_id = new_id("session")
            job = WorkLease(id=new_id("job"), job_type="research_session", payload={"session_id": session_id})
            db.add(job)
            db.flush()
            row = ResearchSession(id=session_id, owner_id=owner_id, request_key=key,
                request_hash=fingerprint, campaign_id=campaign.id, run_id=request.run_id,
                document=document, job_id=job.id)
            db.add(row)
            owner.reserved_cost += request.limits.max_cost
            db.flush()
            return session_view(row)

    def submit(self, owner_id: str, session_id: str, kind: str, key: str,
               payload: dict[str, Any]) -> dict[str, Any]:
        with self.repository.db.session() as db:
            lock_owner(db, owner_id)
            row = owned_session(db, owner_id, session_id)
            fingerprint = content_hash({"kind": kind, "payload": payload})
            prior = db.scalar(select(EpisodeCommand).where(
                EpisodeCommand.session_id == row.id, EpisodeCommand.request_key == key))
            if prior:
                if prior.request_hash != fingerprint:
                    raise ResearchError("idempotency key was reused with different content")
                return command_view(prior)
            if row.state in TERMINAL_SESSIONS and kind not in {"close", "cancel"}:
                raise ResearchError("session is terminal")
            if row.document["mode"] == "managed" and kind in {"reset", "step"}:
                raise ResearchError("managed sessions own their reset and actions")
            pending = db.scalar(select(EpisodeCommand).where(
                EpisodeCommand.session_id == row.id,
                EpisodeCommand.status.in_(["pending", "running"])))
            if pending and kind not in {"cancel", "close"}:
                raise ResearchError("a command is already in progress")
            if kind == "step":
                if row.state != "active" or row.episode_id != payload["episode_id"]:
                    raise ResearchError("reset is required before stepping this episode")
                if row.step_index + 1 != payload["expected_step_index"]:
                    raise ResearchError("unexpected step index")
                generation_id = payload.get("generation_id")
                if generation_id:
                    gen = owned_record(db, owner_id, generation_id, "generation")
                    if gen.document["episode_id"] != row.episode_id or gen.document["session_id"] != row.id:
                        raise ResearchError("generation belongs to another episode")
                    if gen.document.get("parsed_action") != payload["action"]:
                        raise ResearchError("action does not match submitted generation")
            if kind == "reset" and row.document.get("evaluation"):
                if row.episode_id is not None:
                    raise ResearchError("evaluation cases cannot reset an already executed episode")
                if payload.get("seed") not in (None, row.document["seed"]):
                    raise ResearchError("evaluation seed is pinned")
            row.heartbeat_at = datetime.now(UTC)
            command = EpisodeCommand(id=new_id("operation"), session_id=row.id,
                request_key=key, request_hash=fingerprint, kind=kind, payload=payload)
            if row.state in TERMINAL_SESSIONS:
                command.status = "completed"
                command.result = {"state": row.state}
                command.completed_at = datetime.now(UTC)
            elif kind in {"cancel", "close"}:
                row.stop_reason = "client_cancelled" if kind == "cancel" else "client_closed"
            db.add(command)
            db.flush()
            return command_view(command)

    def release(self, db: Session, row: ResearchSession) -> None:
        if row.reservation_released:
            return
        owner = lock_owner(db, row.owner_id)
        campaign = db.get(Campaign, row.campaign_id)
        owner.reserved_cost = max(0, owner.reserved_cost - row.document["limits"]["max_cost"])
        # An uncertain provider call consumes its reservation until an operator reconciles it.
        owner.spent_cost += row.document["limits"]["max_cost"] if row.state == "interrupted" else (campaign.cost_used if campaign else 0)
        row.reservation_released = 1

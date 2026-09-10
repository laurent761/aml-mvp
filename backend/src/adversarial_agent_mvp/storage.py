from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .contracts import (
    CampaignStatus,
    ContainmentResult,
    Decision,
    EpisodeStatus,
    JobStatus,
    PolicyDocument,
    RuntimeContainmentProof,
    new_id,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat()


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


class Base(DeclarativeBase):
    pass


JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class InvalidStateTransition(ValueError):
    """Raised when an aggregate attempts to skip or reverse its lifecycle."""


_OCI_DIGEST = re.compile(r"(?:@|^)(sha256:[0-9a-fA-F]{64})$")


def normalized_image_digest(image: str, supplied: str | None = None) -> str:
    embedded_match = _OCI_DIGEST.search(image)
    embedded = embedded_match.group(1).lower() if embedded_match else None
    provided = supplied.lower() if supplied else None
    if provided and not provided.startswith("sha256:"):
        provided = f"sha256:{provided}"
    if provided and not _OCI_DIGEST.fullmatch(provided):
        raise ValueError("image_digest must be a sha256 digest")
    if embedded and provided and embedded != provided:
        raise ValueError("image digest does not match manifest image")
    digest = embedded or provided
    if digest is None:
        raise ValueError("an immutable OCI sha256 image digest is required")
    return digest


def immutable_image_reference(image: str, digest: str) -> str:
    """Return an OCI reference pinned to ``digest`` rather than a mutable tag."""

    if image.startswith("sha256:"):
        return digest
    if "@" in image:
        name, embedded = image.rsplit("@", 1)
        if not embedded.lower().startswith("sha256:"):
            raise ValueError("image contains an unsupported OCI digest reference")
        return f"{name}@{digest}"

    # A colon after the final slash is a tag. A colon before it belongs to the
    # registry port and must be preserved (for example registry:5000/team/agent).
    final_slash = image.rfind("/")
    final_colon = image.rfind(":")
    name = image[:final_colon] if final_colon > final_slash else image
    if not name:
        raise ValueError("image repository is required for an OCI digest reference")
    return f"{name}@{digest}"


class Target(Base):
    __tablename__ = "targets"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TargetVersion(Base):
    __tablename__ = "target_versions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("targets.id"), index=True)
    image: Mapped[str] = mapped_column(String(500))
    image_digest: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TargetManifestRow(Base):
    __tablename__ = "target_manifests"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    target_version_id: Mapped[str] = mapped_column(ForeignKey("target_versions.id"), unique=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttackTaskRow(Base):
    __tablename__ = "attack_tasks"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    target_version_id: Mapped[str] = mapped_column(ForeignKey("target_versions.id"), index=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScenarioVersionRow(Base):
    __tablename__ = "scenario_versions"
    __table_args__ = (UniqueConstraint("scenario_id", "version", name="uq_scenario_version"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(80))
    family: Mapped[str] = mapped_column(String(100), index=True)
    split: Mapped[str] = mapped_column(String(20), index=True)
    public_document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    private_document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TargetBundleRow(Base):
    __tablename__ = "target_bundles"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    execution_mode: Mapped[str] = mapped_column(String(20))
    scenario_version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"))
    target_version_id: Mapped[str] = mapped_column(ForeignKey("target_versions.id"))
    attack_task_id: Mapped[str] = mapped_column(ForeignKey("attack_tasks.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RedExperimentConfig(Base):
    __tablename__ = "red_experiment_configs"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_red_config_content_hash"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    target_version_id: Mapped[str] = mapped_column(ForeignKey("target_versions.id"), index=True)
    attack_task_id: Mapped[str] = mapped_column(ForeignKey("attack_tasks.id"))
    policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_versions.id"), nullable=True
    )
    search_mode: Mapped[str] = mapped_column(String(20), default="adaptive")
    replay_episode_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    run_kind: Mapped[str] = mapped_column(String(40), default="ATTACK")
    source_finding_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_episode_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    red_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("red_experiment_configs.id"), nullable=True
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    status: Mapped[str] = mapped_column(String(30), default=CampaignStatus.CREATED)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    episodes_started: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_used: Mapped[float] = mapped_column(Float, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Episode(Base):
    __tablename__ = "episodes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    seed: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default=EpisodeStatus.PENDING)
    cumulative_reward: Mapped[float] = mapped_column(Float, default=0)
    terminal_success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EpisodeStep(Base):
    __tablename__ = "episode_steps"
    __table_args__ = (UniqueConstraint("episode_id", "step_index", name="uq_episode_step"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    red_action: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    public_observation: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    reward: Mapped[float] = mapped_column(Float)
    terminal_success: Mapped[bool] = mapped_column(Boolean)
    strategy_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model_invocation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EffectAttemptRow(Base):
    __tablename__ = "effect_attempts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id"), index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("episode_steps.id"), nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyDecisionRow(Base):
    __tablename__ = "policy_decisions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    effect_id: Mapped[str] = mapped_column(ForeignKey("effect_attempts.id"), index=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VerifierEventRow(Base):
    __tablename__ = "verifier_events"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id"), index=True)
    verifier_id: Mapped[str] = mapped_column(String(100))
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id"), index=True)
    verifier_id: Mapped[str] = mapped_column(String(100))
    severity: Mapped[float] = mapped_column(Float)
    evidence_artifact_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("version", name="uq_policy_version_number"),
        UniqueConstraint("content_hash", name="uq_policy_content_hash"),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    policies: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Strategy(Base):
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrajectorySummary(Base):
    __tablename__ = "trajectory_summaries"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id"), index=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelInvocation(Base):
    __tablename__ = "model_invocations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    episode_id: Mapped[str | None] = mapped_column(ForeignKey("episodes.id"), nullable=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("episode_steps.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="SUCCEEDED")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelInvocationLink(Base):
    """Append-only attribution of one model call to one or more executed branches.

    Proposal and ranking calls often influence several adaptive-search branches.  Keeping
    that attribution in a join table ensures the metered invocation is stored and charged
    once while retaining every episode/step that consumed its output.
    """

    __tablename__ = "model_invocation_links"
    __table_args__ = (
        UniqueConstraint(
            "model_invocation_id",
            "step_id",
            name="uq_model_invocation_step_link",
        ),
        Index(
            "uq_model_invocation_episode_only_link",
            "model_invocation_id",
            "episode_id",
            unique=True,
            sqlite_where=text("step_id IS NULL"),
            postgresql_where=text("step_id IS NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    model_invocation_id: Mapped[str] = mapped_column(ForeignKey("model_invocations.id"), index=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id"), index=True)
    step_id: Mapped[str | None] = mapped_column(
        ForeignKey("episode_steps.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkLease(Base):
    __tablename__ = "work_leases"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.PENDING, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    episode_id: Mapped[str | None] = mapped_column(ForeignKey("episodes.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(80))
    uri: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    parent_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OperationalEvent(Base):
    __tablename__ = "operational_events"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(50), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HardeningRun(Base):
    __tablename__ = "hardening_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), index=True)
    policy_version_id: Mapped[str] = mapped_column(ForeignKey("policy_versions.id"))
    exact_replay_campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True
    )
    bypass_campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True
    )
    benign_campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(40), default="PENDING")
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


_IMMUTABLE_MODELS = (
    ScenarioVersionRow,
    TargetBundleRow,
    TargetVersion,
    TargetManifestRow,
    AttackTaskRow,
    RedExperimentConfig,
    EpisodeStep,
    EffectAttemptRow,
    PolicyDecisionRow,
    VerifierEventRow,
    PolicyVersion,
    ModelInvocation,
    ModelInvocationLink,
    Artifact,
    OperationalEvent,
)


def _reject_historical_mutation(_mapper: Any, _connection: Any, target: Any) -> None:
    raise RuntimeError(f"{type(target).__name__} records are append-only")


for _immutable_model in _IMMUTABLE_MODELS:
    event.listen(_immutable_model, "before_update", _reject_historical_mutation)
    event.listen(_immutable_model, "before_delete", _reject_historical_mutation)


CAMPAIGN_TRANSITIONS: dict[str, set[str]] = {
    CampaignStatus.CREATED: {
        CampaignStatus.VALIDATING,
        CampaignStatus.CANCELLING,
        CampaignStatus.FAILED,
        CampaignStatus.REJECTED,
    },
    CampaignStatus.VALIDATING: {
        CampaignStatus.READY,
        CampaignStatus.CANCELLING,
        CampaignStatus.FAILED,
        CampaignStatus.REJECTED,
    },
    CampaignStatus.READY: {
        CampaignStatus.RUNNING,
        CampaignStatus.CANCELLING,
        CampaignStatus.FAILED,
        CampaignStatus.REJECTED,
    },
    CampaignStatus.RUNNING: {
        CampaignStatus.CANCELLING,
        CampaignStatus.FAILED,
        CampaignStatus.COMPLETED,
    },
    CampaignStatus.CANCELLING: {CampaignStatus.FAILED, CampaignStatus.COMPLETED},
    CampaignStatus.FAILED: set(),
    CampaignStatus.COMPLETED: set(),
    CampaignStatus.REJECTED: set(),
}


EPISODE_TRANSITIONS: dict[str, set[str]] = {
    EpisodeStatus.PENDING: {EpisodeStatus.PROVISIONING, EpisodeStatus.FAILED},
    EpisodeStatus.PROVISIONING: {EpisodeStatus.READY, EpisodeStatus.FAILED},
    EpisodeStatus.READY: {EpisodeStatus.EXECUTING, EpisodeStatus.FAILED},
    EpisodeStatus.EXECUTING: {EpisodeStatus.VERIFYING, EpisodeStatus.FAILED},
    EpisodeStatus.VERIFYING: {
        EpisodeStatus.SUCCEEDED,
        EpisodeStatus.EXHAUSTED,
        EpisodeStatus.FAILED,
    },
    EpisodeStatus.SUCCEEDED: {EpisodeStatus.DESTROYED},
    EpisodeStatus.EXHAUSTED: {EpisodeStatus.DESTROYED},
    EpisodeStatus.FAILED: {EpisodeStatus.DESTROYED},
    EpisodeStatus.DESTROYED: set(),
}


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_all(self) -> None:
        from . import research_storage  # noqa: F401

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise


class Repository:
    def __init__(self, database: Database):
        self.db = database

    @staticmethod
    def _append_event(
        session: Session,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> OperationalEvent:
        row = OperationalEvent(
            id=new_id("evt"),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=jsonable(payload or {}),
        )
        session.add(row)
        return row

    def add_event(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> str:
        with self.db.session() as session:
            row = self._append_event(session, aggregate_type, aggregate_id, event_type, payload)
        return row.id

    def create_target(self, name: str) -> Target:
        row = Target(id=new_id("target"), name=name)
        with self.db.session() as session:
            session.add(row)
            self._append_event(session, "target", row.id, "TARGET_CREATED", {"name": name})
        return row

    def list_targets(self) -> list[Target]:
        with self.db.session() as session:
            return list(session.scalars(select(Target).order_by(Target.created_at.desc())))

    def get_target(self, target_id: str) -> Target | None:
        with self.db.session() as session:
            return session.get(Target, target_id)

    def create_target_version(
        self,
        target_id: str,
        image: str,
        manifest: dict[str, Any],
        image_digest: str | None = None,
    ) -> TargetVersion:
        if manifest.get("image") != image:
            raise ValueError("target version image does not match manifest image")
        digest = normalized_image_digest(image, image_digest)
        immutable_image = immutable_image_reference(image, digest)
        immutable_manifest = {**manifest, "image": immutable_image}
        row = TargetVersion(
            id=new_id("targetv"),
            target_id=target_id,
            image=immutable_image,
            image_digest=digest,
        )
        with self.db.session() as session:
            if session.get(Target, target_id) is None:
                raise KeyError("target not found")
            session.add(row)
            session.flush()
            session.add(
                TargetManifestRow(
                    id=new_id("manifest"),
                    target_version_id=row.id,
                    document=jsonable(immutable_manifest),
                )
            )
            self._append_event(
                session,
                "target_version",
                row.id,
                "TARGET_VERSION_CREATED",
                {
                    "target_id": target_id,
                    "image": immutable_image,
                    "image_digest": digest,
                },
            )
        return row

    def list_target_versions(self, target_id: str | None = None) -> list[TargetVersion]:
        statement = select(TargetVersion)
        if target_id:
            statement = statement.where(TargetVersion.target_id == target_id)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(TargetVersion.created_at.desc())))

    def get_target_version(self, target_version_id: str) -> TargetVersion | None:
        with self.db.session() as session:
            return session.get(TargetVersion, target_version_id)

    def create_attack_task(
        self, task_id: str, target_version_id: str, document: dict[str, Any]
    ) -> AttackTaskRow:
        if document.get("target_version_id") != target_version_id:
            raise ValueError("attack task target_version_id is inconsistent")
        row = AttackTaskRow(
            id=task_id, target_version_id=target_version_id, document=jsonable(document)
        )
        with self.db.session() as session:
            if session.get(TargetVersion, target_version_id) is None:
                raise KeyError("target version not found")
            if document.get("scenario_version_id"):
                from .contracts import AttackTask
                from .scenarios import ScenarioCatalog

                ScenarioCatalog(self).runtime(AttackTask.model_validate(document))
            session.add(row)
            self._append_event(
                session,
                "attack_task",
                row.id,
                "ATTACK_TASK_CREATED",
                {"target_version_id": target_version_id},
            )
        return row

    def list_attack_tasks(self, target_version_id: str | None = None) -> list[AttackTaskRow]:
        statement = select(AttackTaskRow)
        if target_version_id:
            statement = statement.where(AttackTaskRow.target_version_id == target_version_id)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(AttackTaskRow.created_at.desc())))

    def get_attack_task(self, task_id: str) -> AttackTaskRow | None:
        with self.db.session() as session:
            return session.get(AttackTaskRow, task_id)

    @staticmethod
    def _red_config_hash(document: dict[str, Any]) -> str:
        hashable = {
            key: value
            for key, value in jsonable(document).items()
            if key not in {"config_id", "created_at"}
        }
        canonical = json.dumps(hashable, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def _ensure_default_red_config(cls, session: Session) -> RedExperimentConfig:
        from .red_contracts import RedExperimentConfig as RedExperimentConfigDocument

        document = RedExperimentConfigDocument().model_dump(
            mode="json", exclude={"config_id", "created_at"}
        )
        digest = cls._red_config_hash(document)
        existing = session.scalar(
            select(RedExperimentConfig).where(RedExperimentConfig.content_hash == digest)
        )
        if existing:
            return existing
        # First requests can arrive concurrently from different research owners.
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        session.execute(insert(RedExperimentConfig).values(
            id=new_id("redcfg"), name="default-mvp", document=document,
            content_hash=digest,
        ).on_conflict_do_nothing(index_elements=["content_hash"]))
        return session.scalars(select(RedExperimentConfig).where(
            RedExperimentConfig.content_hash == digest)).one()

    def save_red_experiment_config(
        self, name: str, document: dict[str, Any]
    ) -> RedExperimentConfig:
        from .red_contracts import RedExperimentConfig as RedExperimentConfigDocument

        normalized = RedExperimentConfigDocument.model_validate(document).model_dump(
            mode="json", exclude={"config_id", "created_at"}
        )
        digest = self._red_config_hash(normalized)
        with self.db.session() as session:
            existing = session.scalar(
                select(RedExperimentConfig).where(RedExperimentConfig.content_hash == digest)
            )
            if existing:
                return existing
            row = RedExperimentConfig(
                id=new_id("redcfg"),
                name=name,
                document=normalized,
                content_hash=digest,
            )
            session.add(row)
            self._append_event(
                session,
                "red_experiment_config",
                row.id,
                "RED_EXPERIMENT_CONFIG_CREATED",
                {"name": name, "content_hash": digest},
            )
        return row

    def default_red_experiment_config(self) -> RedExperimentConfig:
        with self.db.session() as session:
            return self._ensure_default_red_config(session)

    def get_red_experiment_config(self, config_id: str) -> RedExperimentConfig | None:
        with self.db.session() as session:
            return session.get(RedExperimentConfig, config_id)

    def list_red_experiment_configs(self) -> list[RedExperimentConfig]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(RedExperimentConfig).order_by(RedExperimentConfig.created_at.desc())
                )
            )

    def create_campaign(
        self,
        target_version_id: str,
        task_id: str,
        search_mode: str,
        policy_version_id: str | None,
        replay_episode_id: str | None = None,
        *,
        run_kind: str = "ATTACK",
        source_finding_id: str | None = None,
        source_episode_id: str | None = None,
        red_config_id: str | None = None,
        configuration: dict[str, Any] | None = None,
    ) -> Campaign:
        with self.db.session() as session:
            target_version = session.get(TargetVersion, target_version_id)
            if target_version is None:
                raise KeyError("target version not found")
            task = session.get(AttackTaskRow, task_id)
            if task is None:
                raise KeyError("attack task not found")
            if task.target_version_id != target_version_id:
                raise ValueError("campaign target version does not match attack task")
            if task.document.get("target_version_id") != target_version_id:
                raise ValueError("persisted attack task target version is inconsistent")
            if policy_version_id and session.get(PolicyVersion, policy_version_id) is None:
                raise KeyError("policy version not found")
            source_episode_key = source_episode_id or replay_episode_id
            if source_episode_key:
                source_episode = session.get(Episode, source_episode_key)
                if source_episode is None:
                    raise KeyError("source episode not found")
                source_campaign = session.get(Campaign, source_episode.campaign_id)
                if (
                    source_campaign is None
                    or source_campaign.target_version_id != target_version_id
                ):
                    raise ValueError("source episode target version is inconsistent")
            if source_finding_id:
                source_finding = session.get(Finding, source_finding_id)
                if source_finding is None:
                    raise KeyError("source finding not found")
                if source_episode_key and source_finding.episode_id != source_episode_key:
                    raise ValueError("source finding and source episode are inconsistent")
            if red_config_id:
                if session.get(RedExperimentConfig, red_config_id) is None:
                    raise KeyError("Red experiment configuration not found")
            else:
                red_config_id = self._ensure_default_red_config(session).id
            row = Campaign(
                id=new_id("campaign"),
                target_version_id=target_version_id,
                attack_task_id=task_id,
                search_mode=search_mode,
                policy_version_id=policy_version_id,
                replay_episode_id=replay_episode_id,
                run_kind=run_kind,
                source_finding_id=source_finding_id,
                source_episode_id=source_episode_id,
                red_config_id=red_config_id,
                configuration=jsonable(configuration or {}),
            )
            session.add(row)
            session.add(
                WorkLease(id=new_id("job"), job_type="campaign", payload={"campaign_id": row.id})
            )
            self._append_event(
                session,
                "campaign",
                row.id,
                "CAMPAIGN_CREATED",
                {
                    "search_mode": search_mode,
                    "run_kind": run_kind,
                    "red_config_id": red_config_id,
                    "source_finding_id": source_finding_id,
                    "source_episode_id": source_episode_id,
                },
            )
        return row

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        with self.db.session() as session:
            return session.get(Campaign, campaign_id)

    def list_campaigns(self, *, status: str | None = None) -> list[Campaign]:
        statement = select(Campaign)
        if status:
            statement = statement.where(Campaign.status == status)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(Campaign.created_at.desc())))

    def record_containment_preflight(
        self, campaign_id: str, result: ContainmentResult
    ) -> dict[str, Any]:
        """Persist an immutable, scoped proof of the campaign's static preflight."""

        violations = list(result.violations)
        if result.verified == bool(violations):
            raise ValueError("verified containment preflight must have zero violations")
        payload: dict[str, Any] = {
            "check_kind": "STATIC_PREFLIGHT",
            "containment_status": "VERIFIED" if result.verified else "REJECTED",
            "verified": result.verified,
            "zero_violations": not violations,
            "violation_count": len(violations),
            "violations": violations,
        }
        with self.db.session() as session:
            if session.get(Campaign, campaign_id) is None:
                raise KeyError("campaign not found")
            event = self._append_event(
                session,
                "campaign",
                campaign_id,
                "CAMPAIGN_CONTAINMENT_PREFLIGHT",
                payload,
            )
            session.flush()
            payload["event_id"] = event.id
            payload["checked_at"] = utc_iso(event.created_at)
        return payload

    def get_containment_preflight(self, campaign_id: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            event = session.scalar(
                select(OperationalEvent)
                .where(
                    OperationalEvent.aggregate_type == "campaign",
                    OperationalEvent.aggregate_id == campaign_id,
                    OperationalEvent.event_type == "CAMPAIGN_CONTAINMENT_PREFLIGHT",
                )
                .order_by(OperationalEvent.created_at.desc())
                .limit(1)
            )
            if event is None:
                return None
            return {
                **event.payload,
                "event_id": event.id,
                "checked_at": utc_iso(event.created_at),
            }

    def record_runtime_containment(
        self,
        episode_id: str,
        proof: RuntimeContainmentProof,
    ) -> dict[str, Any]:
        """Append a sanitized runtime proof to the episode's immutable event stream."""

        if proof.episode_id != episode_id:
            raise ValueError("runtime containment proof belongs to another episode")
        if not (
            proof.verified
            and proof.network_internal
            and proof.ownership_labels_verified
            and proof.unexpected_attachment_count == 0
            and proof.actual_member_count == proof.expected_member_count
            and set(proof.container_network_counts) == {"blue", "target"}
            and set(proof.container_network_counts.values()) == {1}
        ):
            raise ValueError("runtime containment proof is not a verified boundary")
        payload = {
            **proof.model_dump(mode="json"),
            "containment_status": "VERIFIED",
            "zero_violations": True,
        }
        with self.db.session() as session:
            if session.get(Episode, episode_id) is None:
                raise KeyError("episode not found")
            event = self._append_event(
                session,
                "episode",
                episode_id,
                "EPISODE_RUNTIME_CONTAINMENT_VERIFIED",
                payload,
            )
            session.flush()
            payload["event_id"] = event.id
            payload["recorded_at"] = utc_iso(event.created_at)
        return payload

    def get_runtime_containment(self, episode_id: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            event = session.scalar(
                select(OperationalEvent)
                .where(
                    OperationalEvent.aggregate_type == "episode",
                    OperationalEvent.aggregate_id == episode_id,
                    OperationalEvent.event_type == "EPISODE_RUNTIME_CONTAINMENT_VERIFIED",
                )
                .order_by(OperationalEvent.created_at.desc())
                .limit(1)
            )
            if event is None:
                return None
            return {
                **event.payload,
                "event_id": event.id,
                "recorded_at": utc_iso(event.created_at),
            }

    def set_campaign_status(self, campaign_id: str, status: CampaignStatus, **values: Any) -> None:
        with self.db.session() as session:
            row = session.get(Campaign, campaign_id)
            if row is None:
                raise KeyError("campaign not found")
            current, requested = str(row.status), str(status)
            if current == requested:
                return
            if requested not in CAMPAIGN_TRANSITIONS.get(current, set()):
                raise InvalidStateTransition(f"campaign cannot transition {current} -> {requested}")
            row.status = requested
            for key, value in values.items():
                setattr(row, key, value)
            self._append_event(session, "campaign", campaign_id, f"CAMPAIGN_{requested}", values)

    def cancel_campaign(self, campaign_id: str) -> None:
        with self.db.session() as session:
            row = session.get(Campaign, campaign_id)
            if row is None:
                raise KeyError("campaign not found")
            if row.status in {
                CampaignStatus.COMPLETED,
                CampaignStatus.FAILED,
                CampaignStatus.REJECTED,
            }:
                return
            row.cancellation_requested = True
            row.status = CampaignStatus.CANCELLING
            jobs = list(
                session.scalars(
                    select(WorkLease).where(
                        WorkLease.job_type.in_(["campaign", "episode"]),
                    )
                )
            )
            for job in jobs:
                if (
                    job.payload.get("campaign_id") == campaign_id
                    and job.status == JobStatus.PENDING
                ):
                    job.status = JobStatus.CANCELLED
                    job.owner_id = None
                    job.lease_expires_at = None
            self._append_event(session, "campaign", campaign_id, "CAMPAIGN_CANCELLATION_REQUESTED")
            # A campaign cancelled before any worker owns it would otherwise remain
            # CANCELLING forever because its only queue row is now unclaimable.
            if not any(
                job.payload.get("campaign_id") == campaign_id and job.status == JobStatus.LEASED
                for job in jobs
            ):
                row.status = CampaignStatus.FAILED
                row.completed_at = utcnow()
                self._append_event(
                    session,
                    "campaign",
                    campaign_id,
                    "CAMPAIGN_FAILED",
                    {"reason": "cancelled before worker claim"},
                )

    def add_campaign_usage(
        self,
        campaign_id: str,
        *,
        provider: str,
        model: str,
        tokens: int,
        cost: float,
        request_hash: str,
        episode_id: str | None = None,
        latency_ms: int = 0,
        step_id: str | None = None,
        status: str = "SUCCEEDED",
        error: str | None = None,
        configuration: dict[str, Any] | None = None,
        invocation_id: str | None = None,
    ) -> ModelInvocation:
        with self.db.session() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise KeyError("campaign not found")
            if invocation_id:
                existing = session.get(ModelInvocation, invocation_id)
                if existing:
                    if (
                        existing.campaign_id != campaign_id
                        or existing.episode_id != episode_id
                        or existing.request_hash != request_hash
                        or existing.configuration != jsonable(configuration or {})
                    ):
                        raise ValueError("model invocation ID conflicts with its recorded input")
                    return existing
            linked_episode_id = episode_id
            if step_id:
                step = session.get(EpisodeStep, step_id)
                if step is None:
                    raise KeyError("episode step not found")
                if linked_episode_id is not None and step.episode_id != linked_episode_id:
                    raise ValueError("model invocation step is inconsistent with episode")
                linked_episode_id = step.episode_id
            if linked_episode_id:
                episode = session.get(Episode, linked_episode_id)
                if episode is None or episode.campaign_id != campaign_id:
                    raise ValueError("model invocation episode is inconsistent")
            invocation = ModelInvocation(
                id=invocation_id or new_id("modelcall"),
                campaign_id=campaign_id,
                # Retained as legacy primary attribution during the schema transition.
                # All reads and new multi-branch attribution use ModelInvocationLink.
                episode_id=linked_episode_id,
                step_id=step_id,
                provider=provider,
                model=model,
                tokens=tokens,
                cost=cost,
                latency_ms=latency_ms,
                request_hash=request_hash,
                status=status,
                error=error,
                configuration=jsonable(configuration or {}),
            )
            campaign.tokens_used += tokens
            campaign.cost_used += cost
            session.add(invocation)
            session.flush()
            if linked_episode_id:
                self._link_model_invocation(
                    session,
                    invocation.id,
                    linked_episode_id,
                    step_id=step_id,
                )
        return invocation

    def record_target_inference(self, episode_id: str, records: list[dict[str, Any]]) -> list[str]:
        from .inference_contracts import InferenceAudit, inference_hash

        loaded = self.get_episode(episode_id)
        if loaded is None:
            raise KeyError("inference episode not found")
        ids = []
        for record in records:
            audit = InferenceAudit.model_validate(
                {key: value for key, value in record.items() if key != "sequence"}
            )
            if audit.episode_id != episode_id:
                raise ValueError("inference audit belongs to a different episode")
            invocation = self.add_campaign_usage(
                loaded[0].campaign_id,
                episode_id=episode_id,
                invocation_id="targetcall_" + inference_hash([episode_id, audit.request_id]),
                provider=audit.profile.provider,
                model=audit.profile.model,
                tokens=audit.total_tokens,
                cost=audit.cost,
                latency_ms=audit.latency_ms,
                request_hash=audit.request_hash,
                status=audit.status,
                error=audit.error_code,
                configuration={"role": "target", "target_inference": audit.model_dump(mode="json")},
            )
            ids.append(invocation.id)
        return list(dict.fromkeys(ids))

    @staticmethod
    def _model_invocation_link_id(
        model_invocation_id: str,
        episode_id: str,
        step_id: str | None,
    ) -> str:
        key = f"{model_invocation_id}\0{episode_id}\0{step_id or ''}"
        return f"modelcalllink_{hashlib.sha256(key.encode()).hexdigest()}"

    def _link_model_invocation(
        self,
        session: Session,
        model_invocation_id: str,
        episode_id: str,
        *,
        step_id: str | None = None,
    ) -> ModelInvocationLink:
        invocation = session.get(ModelInvocation, model_invocation_id)
        if invocation is None:
            raise KeyError("model invocation not found")
        episode = session.get(Episode, episode_id)
        if episode is None:
            raise KeyError("episode not found")
        if invocation.campaign_id != episode.campaign_id:
            raise ValueError("model invocation episode is inconsistent with campaign")
        if step_id:
            step = session.get(EpisodeStep, step_id)
            if step is None:
                raise KeyError("episode step not found")
            if step.episode_id != episode_id:
                raise ValueError("model invocation step is inconsistent with episode")

        statement = select(ModelInvocationLink).where(
            ModelInvocationLink.model_invocation_id == model_invocation_id,
            ModelInvocationLink.episode_id == episode_id,
        )
        statement = (
            statement.where(ModelInvocationLink.step_id == step_id)
            if step_id
            else statement.where(ModelInvocationLink.step_id.is_(None))
        )
        existing = session.scalar(statement)
        if existing:
            return existing

        link = ModelInvocationLink(
            id=self._model_invocation_link_id(model_invocation_id, episode_id, step_id),
            model_invocation_id=model_invocation_id,
            episode_id=episode_id,
            step_id=step_id,
        )
        session.add(link)
        self._append_event(
            session,
            "model_invocation",
            model_invocation_id,
            "MODEL_INVOCATION_LINKED",
            {"episode_id": episode_id, "step_id": step_id},
        )
        return link

    def link_model_invocation(
        self,
        model_invocation_id: str,
        episode_id: str,
        *,
        step_id: str | None = None,
    ) -> ModelInvocationLink:
        """Idempotently attribute an existing invocation to an episode or step."""

        with self.db.session() as session:
            return self._link_model_invocation(
                session,
                model_invocation_id,
                episode_id,
                step_id=step_id,
            )

    def link_model_invocations(
        self,
        model_invocation_ids: list[str],
        episode_id: str,
        *,
        step_id: str | None = None,
    ) -> list[ModelInvocationLink]:
        """Atomically link several shared proposal/ranking calls to one branch step."""

        with self.db.session() as session:
            return [
                self._link_model_invocation(
                    session,
                    invocation_id,
                    episode_id,
                    step_id=step_id,
                )
                for invocation_id in dict.fromkeys(model_invocation_ids)
            ]

    def create_episode(self, campaign_id: str, seed: int) -> Episode:
        row = Episode(id=new_id("episode"), campaign_id=campaign_id, seed=seed)
        with self.db.session() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise KeyError("campaign not found")
            if campaign.cancellation_requested:
                raise InvalidStateTransition("cannot create an episode for a cancelling campaign")
            session.add(row)
            campaign.episodes_started += 1
            self._append_event(
                session,
                "episode",
                row.id,
                "EPISODE_CREATED",
                {
                    "campaign_id": campaign_id,
                    "seed": seed,
                    "policy_version_id": campaign.policy_version_id,
                },
            )
        return row

    def set_episode_status(self, episode_id: str, status: EpisodeStatus, **values: Any) -> None:
        with self.db.session() as session:
            row = session.get(Episode, episode_id)
            if row is None:
                raise KeyError("episode not found")
            current, requested = str(row.status), str(status)
            if current == requested:
                return
            if requested not in EPISODE_TRANSITIONS.get(current, set()):
                raise InvalidStateTransition(f"episode cannot transition {current} -> {requested}")
            row.status = requested
            for key, value in values.items():
                setattr(row, key, value)
            self._append_event(session, "episode", episode_id, f"EPISODE_{requested}", values)

    def add_step(
        self,
        episode_id: str,
        step_index: int,
        action: dict[str, Any],
        observation: dict[str, Any],
        reward: float,
        terminal: bool,
        strategy_id: str | None = None,
        model_invocation_id: str | None = None,
    ) -> EpisodeStep:
        row = EpisodeStep(
            id=new_id("step"),
            episode_id=episode_id,
            step_index=step_index,
            red_action=jsonable(action),
            public_observation=jsonable(observation),
            reward=reward,
            terminal_success=terminal,
            strategy_id=strategy_id,
            model_invocation_id=model_invocation_id,
        )
        with self.db.session() as session:
            if session.get(Episode, episode_id) is None:
                raise KeyError("episode not found")
            session.add(row)
            session.flush()
            if model_invocation_id:
                self._link_model_invocation(
                    session,
                    model_invocation_id,
                    episode_id,
                    step_id=row.id,
                )
            self._append_event(
                session,
                "episode",
                episode_id,
                "EPISODE_STEP_RECORDED",
                {"step_id": row.id, "step_index": step_index, "terminal_success": terminal},
            )
        return row

    def get_episode_step(self, episode_id: str, step_index: int) -> EpisodeStep | None:
        with self.db.session() as session:
            return session.scalar(
                select(EpisodeStep).where(
                    EpisodeStep.episode_id == episode_id,
                    EpisodeStep.step_index == step_index,
                )
            )

    def get_episode(self, episode_id: str) -> tuple[Episode, list[EpisodeStep]] | None:
        with self.db.session() as session:
            episode = session.get(Episode, episode_id)
            if episode is None:
                return None
            steps = list(
                session.scalars(
                    select(EpisodeStep)
                    .where(EpisodeStep.episode_id == episode_id)
                    .order_by(EpisodeStep.step_index)
                )
            )
            return episode, steps

    def list_episodes(self, campaign_id: str | None = None) -> list[Episode]:
        statement = select(Episode)
        if campaign_id:
            statement = statement.where(Episode.campaign_id == campaign_id)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(Episode.created_at.desc())))

    def record_step_graph(
        self,
        *,
        episode_id: str,
        step_index: int,
        action: dict[str, Any],
        observation: dict[str, Any],
        reward: float,
        terminal: bool,
        strategy_id: str | None = None,
        model_invocation: dict[str, Any] | None = None,
        model_invocation_ids: list[str] | None = None,
        effects: list[dict[str, Any]] | None = None,
        verifier_events: list[dict[str, Any]] | None = None,
    ) -> EpisodeStep:
        """Append one complete causal step in a single database transaction.

        Each effect item may be a raw effect document or an envelope containing
        ``effect``, ``decision`` and ``result`` documents.
        """
        step_id = new_id("step")
        invocation_id = new_id("modelcall") if model_invocation else None
        linked_invocation_ids = list(dict.fromkeys(model_invocation_ids or []))
        if invocation_id:
            linked_invocation_ids.insert(0, invocation_id)
        step = EpisodeStep(
            id=step_id,
            episode_id=episode_id,
            step_index=step_index,
            red_action=jsonable(action),
            public_observation=jsonable(observation),
            reward=reward,
            terminal_success=terminal,
            strategy_id=strategy_id,
            model_invocation_id=(linked_invocation_ids[0] if linked_invocation_ids else None),
        )
        with self.db.session() as session:
            episode = session.get(Episode, episode_id)
            if episode is None:
                raise KeyError("episode not found")
            campaign = session.get(Campaign, episode.campaign_id)
            if campaign is None:
                raise KeyError("campaign not found")
            session.add(step)
            session.flush()
            if model_invocation:
                invocation = ModelInvocation(
                    id=str(invocation_id),
                    campaign_id=campaign.id,
                    episode_id=episode_id,
                    step_id=step_id,
                    provider=str(model_invocation.get("provider", "unknown")),
                    model=str(model_invocation.get("model", "unknown")),
                    tokens=int(model_invocation.get("tokens", 0)),
                    cost=float(model_invocation.get("cost", 0)),
                    latency_ms=int(model_invocation.get("latency_ms", 0)),
                    request_hash=str(model_invocation.get("request_hash", "")),
                    status=str(model_invocation.get("status", "SUCCEEDED")),
                    error=model_invocation.get("error"),
                    configuration=jsonable(model_invocation.get("configuration", {})),
                )
                campaign.tokens_used += invocation.tokens
                campaign.cost_used += invocation.cost
                session.add(invocation)
                session.flush()
            for linked_invocation_id in linked_invocation_ids:
                self._link_model_invocation(
                    session,
                    linked_invocation_id,
                    episode_id,
                    step_id=step_id,
                )
            for envelope in effects or []:
                effect_document = jsonable(envelope.get("effect", envelope))
                effect_id = str(effect_document.get("effect_id") or new_id("effect"))
                effect_document["effect_id"] = effect_id
                effect_document["episode_id"] = episode_id
                session.add(
                    EffectAttemptRow(
                        id=effect_id,
                        episode_id=episode_id,
                        step_id=step_id,
                        document=effect_document,
                    )
                )
                if envelope.get("decision") is not None:
                    session.add(
                        PolicyDecisionRow(
                            id=new_id("decision"),
                            effect_id=effect_id,
                            document=jsonable(envelope["decision"]),
                            result=jsonable(envelope.get("result", {})),
                        )
                    )
            for signal in verifier_events or []:
                signal_document = jsonable(signal)
                session.add(
                    VerifierEventRow(
                        id=new_id("verifierevt"),
                        episode_id=episode_id,
                        verifier_id=str(signal_document.get("verifier_id", "unknown")),
                        document=signal_document,
                    )
                )
            self._append_event(
                session,
                "episode",
                episode_id,
                "EPISODE_STEP_GRAPH_RECORDED",
                {
                    "step_id": step_id,
                    "step_index": step_index,
                    "effect_count": len(effects or []),
                    "verifier_event_count": len(verifier_events or []),
                },
            )
        return step

    def add_effect_attempt(
        self, episode_id: str, document: dict[str, Any], step_id: str | None = None
    ) -> EffectAttemptRow:
        payload = jsonable(document)
        effect_id = str(payload.get("effect_id") or new_id("effect"))
        payload["effect_id"] = effect_id
        payload["episode_id"] = episode_id
        row = EffectAttemptRow(
            id=effect_id, episode_id=episode_id, step_id=step_id, document=payload
        )
        with self.db.session() as session:
            if session.get(Episode, episode_id) is None:
                raise KeyError("episode not found")
            session.add(row)
        return row

    def add_policy_decision(
        self,
        effect_id: str,
        decision: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> PolicyDecisionRow:
        row = PolicyDecisionRow(
            id=new_id("decision"),
            effect_id=effect_id,
            document=jsonable(decision),
            result=jsonable(result or {}),
        )
        with self.db.session() as session:
            if session.get(EffectAttemptRow, effect_id) is None:
                raise KeyError("effect attempt not found")
            session.add(row)
        return row

    def add_verifier_event(
        self, episode_id: str, verifier_id: str, document: dict[str, Any]
    ) -> VerifierEventRow:
        row = VerifierEventRow(
            id=new_id("verifierevt"),
            episode_id=episode_id,
            verifier_id=verifier_id,
            document=jsonable(document),
        )
        with self.db.session() as session:
            if session.get(Episode, episode_id) is None:
                raise KeyError("episode not found")
            session.add(row)
        return row

    def add_trajectory_summary(
        self, episode_id: str, document: dict[str, Any], score: float
    ) -> TrajectorySummary:
        row = TrajectorySummary(
            id=str(document.get("trajectory_id") or new_id("trajectory")),
            episode_id=episode_id,
            document=jsonable(document),
            score=score,
        )
        with self.db.session() as session:
            if session.get(Episode, episode_id) is None:
                raise KeyError("episode not found")
            session.add(row)
        return row

    def save_strategy(self, strategy_id: str, document: dict[str, Any]) -> Strategy:
        normalized = jsonable(document)
        row = Strategy(id=strategy_id, document=normalized)
        with self.db.session() as session:
            existing = session.get(Strategy, strategy_id)
            if existing:
                existing.document = normalized
                self._append_event(
                    session,
                    "strategy",
                    strategy_id,
                    "STRATEGY_UPDATED",
                    {"attempt_count": normalized.get("attempt_count")},
                )
                return existing
            session.add(row)
            self._append_event(
                session,
                "strategy",
                strategy_id,
                "STRATEGY_CREATED",
                {"attempt_count": normalized.get("attempt_count")},
            )
        return row

    def list_strategies(self) -> list[Strategy]:
        with self.db.session() as session:
            return list(session.scalars(select(Strategy).order_by(Strategy.created_at.desc())))

    def get_strategy(self, strategy_id: str) -> Strategy | None:
        with self.db.session() as session:
            return session.get(Strategy, strategy_id)

    def list_trajectory_summaries(self, episode_id: str | None = None) -> list[TrajectorySummary]:
        statement = select(TrajectorySummary)
        if episode_id:
            statement = statement.where(TrajectorySummary.episode_id == episode_id)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(TrajectorySummary.created_at)))

    def list_model_invocations(
        self,
        *,
        campaign_id: str | None = None,
        episode_id: str | None = None,
        step_id: str | None = None,
    ) -> list[ModelInvocation]:
        statement = select(ModelInvocation)
        if episode_id or step_id:
            statement = statement.join(
                ModelInvocationLink,
                ModelInvocationLink.model_invocation_id == ModelInvocation.id,
            )
        if campaign_id:
            statement = statement.where(ModelInvocation.campaign_id == campaign_id)
        if episode_id:
            statement = statement.where(ModelInvocationLink.episode_id == episode_id)
        if step_id:
            statement = statement.where(ModelInvocationLink.step_id == step_id)
        with self.db.session() as session:
            return list(session.scalars(statement.distinct().order_by(ModelInvocation.created_at)))

    def list_model_invocation_links(
        self,
        *,
        model_invocation_id: str | None = None,
        episode_id: str | None = None,
        step_id: str | None = None,
    ) -> list[ModelInvocationLink]:
        statement = select(ModelInvocationLink)
        if model_invocation_id:
            statement = statement.where(
                ModelInvocationLink.model_invocation_id == model_invocation_id
            )
        if episode_id:
            statement = statement.where(ModelInvocationLink.episode_id == episode_id)
        if step_id:
            statement = statement.where(ModelInvocationLink.step_id == step_id)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(ModelInvocationLink.created_at)))

    def list_effect_attempts(self, episode_id: str) -> list[EffectAttemptRow]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(EffectAttemptRow)
                    .where(EffectAttemptRow.episode_id == episode_id)
                    .order_by(EffectAttemptRow.created_at)
                )
            )

    def list_policy_decisions(self, episode_id: str) -> list[PolicyDecisionRow]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(PolicyDecisionRow)
                    .join(EffectAttemptRow, PolicyDecisionRow.effect_id == EffectAttemptRow.id)
                    .where(EffectAttemptRow.episode_id == episode_id)
                    .order_by(PolicyDecisionRow.created_at)
                )
            )

    def list_verifier_events(self, episode_id: str) -> list[VerifierEventRow]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(VerifierEventRow)
                    .where(VerifierEventRow.episode_id == episode_id)
                    .order_by(VerifierEventRow.created_at)
                )
            )

    def add_finding(
        self,
        campaign_id: str,
        episode_id: str,
        verifier_id: str,
        severity: float,
        evidence_artifact_id: str | None = None,
    ) -> Finding:
        row = Finding(
            id=new_id("finding"),
            campaign_id=campaign_id,
            episode_id=episode_id,
            verifier_id=verifier_id,
            severity=severity,
            evidence_artifact_id=evidence_artifact_id,
        )
        with self.db.session() as session:
            episode = session.get(Episode, episode_id)
            if episode is None or episode.campaign_id != campaign_id:
                raise ValueError("finding episode is inconsistent with campaign")
            session.add(row)
            self._append_event(
                session,
                "finding",
                row.id,
                "FINDING_CREATED",
                {
                    "campaign_id": campaign_id,
                    "episode_id": episode_id,
                    "verifier_id": verifier_id,
                    "severity": severity,
                },
            )
        return row

    def list_findings(
        self,
        *,
        campaign_id: str | None = None,
        episode_id: str | None = None,
        status: str | None = None,
    ) -> list[Finding]:
        statement = select(Finding)
        if campaign_id:
            statement = statement.where(Finding.campaign_id == campaign_id)
        if episode_id:
            statement = statement.where(Finding.episode_id == episode_id)
        if status:
            statement = statement.where(Finding.status == status)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(Finding.created_at.desc())))

    def get_finding(self, finding_id: str) -> Finding | None:
        with self.db.session() as session:
            return session.get(Finding, finding_id)

    def associate_finding_policy(self, finding_id: str, policy_version_id: str) -> Finding:
        with self.db.session() as session:
            finding = session.get(Finding, finding_id)
            if finding is None:
                raise KeyError("finding not found")
            if session.get(PolicyVersion, policy_version_id) is None:
                raise KeyError("policy version not found")
            finding.policy_version_id = policy_version_id
            finding.status = "HARDENING"
            self._append_event(
                session,
                "finding",
                finding_id,
                "FINDING_POLICY_ASSOCIATED",
                {"policy_version_id": policy_version_id},
            )
            return finding

    def link_episode_findings_to_evidence(self, episode_id: str, artifact_id: str) -> list[str]:
        with self.db.session() as session:
            if session.get(Artifact, artifact_id) is None:
                raise KeyError("artifact not found")
            findings = list(
                session.scalars(select(Finding).where(Finding.episode_id == episode_id))
            )
            for finding in findings:
                if finding.evidence_artifact_id is None:
                    finding.evidence_artifact_id = artifact_id
                    self._append_event(
                        session,
                        "finding",
                        finding.id,
                        "FINDING_EVIDENCE_LINKED",
                        {"artifact_id": artifact_id},
                    )
            return [finding.id for finding in findings]

    def enqueue(self, job_type: str, payload: dict[str, Any]) -> WorkLease:
        row = WorkLease(id=new_id("job"), job_type=job_type, payload=payload)
        with self.db.session() as session:
            session.add(row)
        return row

    @staticmethod
    def _campaign_id_for_job(row: WorkLease) -> str | None:
        value = row.payload.get("campaign_id")
        return value if isinstance(value, str) and value else None

    @classmethod
    def _fail_stale_episodes(
        cls,
        session: Session,
        campaign_id: str,
        *,
        reason: str,
    ) -> list[str]:
        """Fail crash-interrupted episodes before another campaign attempt runs.

        Only non-terminal rows are selected, so repeated recovery passes do not
        create duplicate terminal events.
        """

        nonterminal = {
            EpisodeStatus.PENDING,
            EpisodeStatus.PROVISIONING,
            EpisodeStatus.READY,
            EpisodeStatus.EXECUTING,
            EpisodeStatus.VERIFYING,
        }
        episodes = list(
            session.scalars(
                select(Episode).where(
                    Episode.campaign_id == campaign_id,
                    Episode.status.in_(nonterminal),
                )
            )
        )
        completed_at = utcnow()
        for episode in episodes:
            episode.status = EpisodeStatus.FAILED
            episode.error = reason
            episode.completed_at = completed_at
            cls._append_event(
                session,
                "episode",
                episode.id,
                "EPISODE_FAILED",
                {"error": reason, "recovered": True},
            )
        return [episode.id for episode in episodes]

    @classmethod
    def _reconcile_failed_campaign_job(
        cls,
        session: Session,
        row: WorkLease,
        *,
        reason: str,
    ) -> None:
        campaign_id = cls._campaign_id_for_job(row)
        if campaign_id is None:
            return
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            return
        cls._fail_stale_episodes(session, campaign_id, reason=reason)
        if campaign.status in {
            CampaignStatus.COMPLETED,
            CampaignStatus.FAILED,
            CampaignStatus.REJECTED,
        }:
            return
        campaign.status = CampaignStatus.FAILED
        campaign.completed_at = utcnow()
        cls._append_event(
            session,
            "campaign",
            campaign_id,
            "CAMPAIGN_FAILED",
            {"reason": reason, "job_id": row.id, "attempt_count": row.attempt_count},
        )

    @classmethod
    def _terminally_fail_job(
        cls,
        session: Session,
        row: WorkLease,
        *,
        reason: str,
    ) -> None:
        row.status = JobStatus.FAILED
        row.owner_id = None
        row.lease_expires_at = None
        row.last_error = reason
        cls._append_event(
            session,
            "work_lease",
            row.id,
            "JOB_FAILED",
            {"reason": reason, "attempt_count": row.attempt_count},
        )
        cls._reconcile_failed_campaign_job(session, row, reason=reason)

    def claim_job(
        self,
        owner_id: str,
        lease_seconds: int,
        job_type: str = "campaign",
        *,
        max_attempts: int = 3,
    ) -> WorkLease | None:
        now, expires = utcnow(), utcnow() + timedelta(seconds=lease_seconds)
        with self.db.session() as session:
            expired_terminal_query = select(WorkLease).where(
                WorkLease.job_type == job_type,
                WorkLease.status == JobStatus.LEASED,
                WorkLease.lease_expires_at < now,
                WorkLease.attempt_count >= max_attempts,
            )
            if session.bind and session.bind.dialect.name == "postgresql":
                expired_terminal_query = expired_terminal_query.with_for_update(skip_locked=True)
            for expired in session.scalars(expired_terminal_query):
                self._terminally_fail_job(
                    session,
                    expired,
                    reason="worker lease expired after maximum attempts",
                )
            candidate_query = (
                select(WorkLease)
                .where(
                    WorkLease.job_type == job_type,
                    WorkLease.attempt_count < max_attempts,
                    or_(WorkLease.available_at.is_(None), WorkLease.available_at <= now),
                    (
                        (WorkLease.status == JobStatus.PENDING)
                        | (
                            (WorkLease.status == JobStatus.LEASED)
                            & (WorkLease.lease_expires_at < now)
                        )
                    ),
                )
                .order_by(WorkLease.created_at)
                .limit(1)
            )
            if session.bind and session.bind.dialect.name == "postgresql":
                candidate_query = candidate_query.with_for_update(skip_locked=True)
            candidate = session.scalar(candidate_query)
            if candidate is None:
                return None
            if candidate.attempt_count:
                campaign_id = self._campaign_id_for_job(candidate)
                if campaign_id:
                    self._fail_stale_episodes(
                        session,
                        campaign_id,
                        reason="episode interrupted before worker retry",
                    )
            statement = (
                update(WorkLease)
                .where(
                    WorkLease.id == candidate.id,
                    ((WorkLease.status == JobStatus.PENDING) | (WorkLease.lease_expires_at < now)),
                )
                .values(
                    status=JobStatus.LEASED,
                    owner_id=owner_id,
                    lease_expires_at=expires,
                    available_at=now,
                    attempt_count=WorkLease.attempt_count + 1,
                )
                .execution_options(synchronize_session=False)
            )
            claimed = cast(CursorResult[Any], session.execute(statement))
            if claimed.rowcount != 1:
                return None
            session.flush()
            session.expire_all()
            claimed_row = session.get(WorkLease, candidate.id)
            if claimed_row is not None:
                self._append_event(
                    session,
                    "work_lease",
                    claimed_row.id,
                    "JOB_LEASED",
                    {"owner_id": owner_id, "attempt_count": claimed_row.attempt_count},
                )
            return claimed_row

    def heartbeat(self, job_id: str, owner_id: str, lease_seconds: int) -> bool:
        with self.db.session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(WorkLease)
                    .where(
                        WorkLease.id == job_id,
                        WorkLease.owner_id == owner_id,
                        WorkLease.status == JobStatus.LEASED,
                    )
                    .values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds))
                ),
            )
            return result.rowcount == 1

    def finish_job(self, job_id: str, owner_id: str, *, error: str | None = None) -> None:
        with self.db.session() as session:
            row = session.get(WorkLease, job_id)
            if row is None or row.owner_id != owner_id:
                raise KeyError("owned job not found")
            if error:
                self._terminally_fail_job(session, row, reason=error)
                return
            row.status = JobStatus.COMPLETED
            row.owner_id = None
            row.last_error = None
            row.lease_expires_at = None
            self._append_event(
                session,
                "work_lease",
                row.id,
                "JOB_COMPLETED",
                {"attempt_count": row.attempt_count},
            )

    def fail_or_retry_job(
        self,
        job_id: str,
        owner_id: str,
        *,
        error: str,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
    ) -> JobStatus:
        """Release a failed lease for bounded retry or reconcile terminal failure."""

        with self.db.session() as session:
            row = session.get(WorkLease, job_id)
            if row is None or row.owner_id != owner_id or row.status != JobStatus.LEASED:
                raise KeyError("owned leased job not found")
            if row.attempt_count >= max_attempts:
                self._terminally_fail_job(session, row, reason=error)
                return JobStatus.FAILED

            delay = min(
                retry_max_seconds,
                retry_base_seconds * (2 ** max(0, row.attempt_count - 1)),
            )
            row.status = JobStatus.PENDING
            row.owner_id = None
            row.lease_expires_at = None
            row.available_at = utcnow() + timedelta(seconds=delay)
            row.last_error = error
            self._append_event(
                session,
                "work_lease",
                row.id,
                "JOB_RETRY_SCHEDULED",
                {
                    "attempt_count": row.attempt_count,
                    "delay_seconds": delay,
                    "error": error,
                },
            )
            return JobStatus.PENDING

    def load_task(self, task_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            row = session.get(AttackTaskRow, task_id)
            if row is None:
                raise KeyError("attack task not found")
            return row.document

    def load_manifest(self, target_version_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            row = session.scalar(
                select(TargetManifestRow).where(
                    TargetManifestRow.target_version_id == target_version_id
                )
            )
            if row is None:
                raise KeyError("manifest not found")
            return row.document

    def policy_documents(self, policy_version_id: str | None) -> list[dict[str, Any]]:
        if policy_version_id is None:
            return []
        with self.db.session() as session:
            row = session.get(PolicyVersion, policy_version_id)
            if row is None:
                raise KeyError("policy version not found")
            return row.policies

    def save_policy_version(
        self,
        policies: list[dict[str, Any]],
        content_hash: str | None = None,
    ) -> PolicyVersion:
        documents = [PolicyDocument.model_validate(policy) for policy in policies]
        if any(document.decision == Decision.ALLOW_REAL for document in documents):
            raise ValueError("ALLOW_REAL is invalid for capsule policy versions")
        policy_ids = [document.policy_id for document in documents]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("policy_id values must be unique within a policy version")
        canonical_policies = [document.model_dump(mode="json") for document in documents]
        computed_hash = hashlib.sha256(
            json.dumps(canonical_policies, sort_keys=True).encode()
        ).hexdigest()
        if content_hash is not None and content_hash != computed_hash:
            raise ValueError("policy content hash does not match the validated document")
        content_hash = computed_hash
        with self.db.session() as session:
            existing = session.scalar(
                select(PolicyVersion).where(PolicyVersion.content_hash == content_hash)
            )
            if existing:
                return existing
            latest = (
                session.scalar(
                    select(PolicyVersion.version).order_by(PolicyVersion.version.desc()).limit(1)
                )
                or 0
            )
            row = PolicyVersion(
                id=new_id("policyv"),
                version=latest + 1,
                policies=canonical_policies,
                content_hash=content_hash,
            )
            session.add(row)
            self._append_event(
                session,
                "policy_version",
                row.id,
                "POLICY_VERSION_CREATED",
                {"version": row.version, "content_hash": content_hash},
            )
        return row

    def get_policy_version(self, policy_version_id: str) -> PolicyVersion | None:
        with self.db.session() as session:
            return session.get(PolicyVersion, policy_version_id)

    def list_policy_versions(self) -> list[PolicyVersion]:
        with self.db.session() as session:
            return list(
                session.scalars(select(PolicyVersion).order_by(PolicyVersion.version.desc()))
            )

    def save_artifact(
        self,
        *,
        artifact_id: str,
        kind: str,
        uri: str,
        sha256: str,
        size_bytes: int,
        campaign_id: str | None = None,
        episode_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        parent_artifact_id: str | None = None,
    ) -> Artifact:
        row = Artifact(
            id=artifact_id,
            kind=kind,
            uri=uri,
            sha256=sha256,
            size_bytes=size_bytes,
            campaign_id=campaign_id,
            episode_id=episode_id,
            metadata_json=metadata or {},
            parent_artifact_id=parent_artifact_id,
        )
        with self.db.session() as session:
            if parent_artifact_id and session.get(Artifact, parent_artifact_id) is None:
                raise KeyError("parent artifact not found")
            session.add(row)
            self._append_event(
                session,
                "artifact",
                row.id,
                "ARTIFACT_CREATED",
                {
                    "kind": kind,
                    "campaign_id": campaign_id,
                    "episode_id": episode_id,
                    "sha256": sha256,
                },
            )
        return row

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self.db.session() as session:
            return session.get(Artifact, artifact_id)

    def list_artifacts(
        self,
        *,
        campaign_id: str | None = None,
        episode_id: str | None = None,
        kind: str | None = None,
    ) -> list[Artifact]:
        statement = select(Artifact)
        if campaign_id:
            statement = statement.where(Artifact.campaign_id == campaign_id)
        if episode_id:
            statement = statement.where(Artifact.episode_id == episode_id)
        if kind:
            statement = statement.where(Artifact.kind == kind)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(Artifact.created_at.desc())))

    def list_events(
        self,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[OperationalEvent]:
        statement = select(OperationalEvent)
        if aggregate_type:
            statement = statement.where(OperationalEvent.aggregate_type == aggregate_type)
        if aggregate_id:
            statement = statement.where(OperationalEvent.aggregate_id == aggregate_id)
        with self.db.session() as session:
            return list(
                session.scalars(
                    statement.order_by(OperationalEvent.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )

    def get_event(self, event_id: str) -> OperationalEvent | None:
        with self.db.session() as session:
            return session.get(OperationalEvent, event_id)

    def campaign_metrics(self, campaign_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise KeyError("campaign not found")
            episodes = list(
                session.scalars(select(Episode).where(Episode.campaign_id == campaign_id))
            )
            episode_ids = [episode.id for episode in episodes]
            finding_count = len(
                list(session.scalars(select(Finding.id).where(Finding.campaign_id == campaign_id)))
            )
            step_count = 0
            effect_count = 0
            if episode_ids:
                step_count = len(
                    list(
                        session.scalars(
                            select(EpisodeStep.id).where(EpisodeStep.episode_id.in_(episode_ids))
                        )
                    )
                )
                effect_count = len(
                    list(
                        session.scalars(
                            select(EffectAttemptRow.id).where(
                                EffectAttemptRow.episode_id.in_(episode_ids)
                            )
                        )
                    )
                )
            return {
                "campaign_id": campaign.id,
                "status": campaign.status,
                "episodes_started": campaign.episodes_started,
                "episodes_recorded": len(episodes),
                "successful_episodes": sum(episode.terminal_success for episode in episodes),
                "step_count": step_count,
                "effect_count": effect_count,
                "finding_count": finding_count,
                "tokens_used": campaign.tokens_used,
                "cost_used": campaign.cost_used,
            }

    def create_hardening_run(
        self,
        finding_id: str,
        policy_version_id: str,
        *,
        exact_replay_campaign_id: str | None = None,
        bypass_campaign_id: str | None = None,
        benign_campaign_id: str | None = None,
    ) -> HardeningRun:
        with self.db.session() as session:
            finding = session.get(Finding, finding_id)
            if finding is None:
                raise KeyError("finding not found")
            if session.get(PolicyVersion, policy_version_id) is None:
                raise KeyError("policy version not found")
            row = HardeningRun(
                id=new_id("hardening"),
                finding_id=finding_id,
                policy_version_id=policy_version_id,
                exact_replay_campaign_id=exact_replay_campaign_id,
                bypass_campaign_id=bypass_campaign_id,
                benign_campaign_id=benign_campaign_id,
            )
            finding.policy_version_id = policy_version_id
            finding.status = "HARDENING"
            session.add(row)
            self._append_event(
                session,
                "hardening_run",
                row.id,
                "HARDENING_RUN_CREATED",
                {"finding_id": finding_id, "policy_version_id": policy_version_id},
            )
        return row

    def get_hardening_run(self, run_id: str) -> HardeningRun | None:
        with self.db.session() as session:
            return session.get(HardeningRun, run_id)

    def list_hardening_runs(self, finding_id: str | None = None) -> list[HardeningRun]:
        statement = select(HardeningRun)
        if finding_id:
            statement = statement.where(HardeningRun.finding_id == finding_id)
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(HardeningRun.created_at.desc())))

    def list_hardening_runs_for_campaign(self, campaign_id: str) -> list[HardeningRun]:
        statement = select(HardeningRun).where(
            or_(
                HardeningRun.exact_replay_campaign_id == campaign_id,
                HardeningRun.bypass_campaign_id == campaign_id,
                HardeningRun.benign_campaign_id == campaign_id,
            )
        )
        with self.db.session() as session:
            return list(session.scalars(statement.order_by(HardeningRun.created_at)))

    def complete_hardening_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any],
        artifact_id: str | None = None,
    ) -> HardeningRun:
        with self.db.session() as session:
            row = session.get(HardeningRun, run_id)
            if row is None:
                raise KeyError("hardening run not found")
            if artifact_id and session.get(Artifact, artifact_id) is None:
                raise KeyError("artifact not found")
            row.status = status
            row.result = jsonable(result)
            row.artifact_id = artifact_id
            row.completed_at = utcnow()
            finding = session.get(Finding, row.finding_id)
            if finding:
                if status == "PASSED":
                    finding.status = "HARDENED"
                elif status == "BYPASS_FOUND":
                    finding.status = "BYPASS_FOUND"
                else:
                    # An incomplete or unproven hardening run is not proof of a bypass.
                    finding.status = "HARDENING"
            self._append_event(
                session,
                "hardening_run",
                run_id,
                f"HARDENING_RUN_{status}",
                {"artifact_id": artifact_id},
            )
            return row

    def link_hardening_artifact(self, run_id: str, artifact_id: str) -> HardeningRun:
        with self.db.session() as session:
            row = session.get(HardeningRun, run_id)
            if row is None:
                raise KeyError("hardening run not found")
            if session.get(Artifact, artifact_id) is None:
                raise KeyError("artifact not found")
            row.artifact_id = artifact_id
            self._append_event(
                session,
                "hardening_run",
                run_id,
                "HARDENING_EVIDENCE_LINKED",
                {"artifact_id": artifact_id},
            )
            return row

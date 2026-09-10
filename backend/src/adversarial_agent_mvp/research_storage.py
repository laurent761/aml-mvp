"""Durable ownership, commands and immutable research lineage."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from .storage import JSON_DOCUMENT, Base, _reject_historical_mutation, utcnow


class ResearchRecord(Base):
    __tablename__ = "research_records"
    __table_args__ = (UniqueConstraint("owner_id", "kind", "request_key", name="uq_research_request"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    request_key: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchOwner(Base):
    __tablename__ = "research_owners"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    reserved_cost: Mapped[float] = mapped_column(Float, default=0)
    spent_cost: Mapped[float] = mapped_column(Float, default=0)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)


class ResearchSession(Base):
    __tablename__ = "research_sessions"
    __table_args__ = (UniqueConstraint("owner_id", "request_key", name="uq_session_request"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    request_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), unique=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("research_records.id"), nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    episode_id: Mapped[str | None] = mapped_column(ForeignKey("episodes.id"), nullable=True)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("work_leases.id"))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stop_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    capsule_handle: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    reservation_released: Mapped[int] = mapped_column(Integer, default=0)


class EpisodeCommand(Base):
    __tablename__ = "episode_commands"
    __table_args__ = (UniqueConstraint("session_id", "request_key", name="uq_command_request"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("research_sessions.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    fence: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArtifactUpload(Base):
    __tablename__ = "artifact_uploads"
    __table_args__ = (UniqueConstraint("owner_id", "request_key", name="uq_upload_request"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    request_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


event.listen(ResearchRecord, "before_update", _reject_historical_mutation)
event.listen(ResearchRecord, "before_delete", _reject_historical_mutation)

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Request
from sqlalchemy import select

from .contracts import new_id
from .research import (
    ADMIN_OWNER_ID,
    ResearchError,
    ResearchService,
    lock_owner,
    owned_record,
    record_view,
)
from .research_api import request_key
from .research_contracts import DatasetCreate, UploadCreate
from .research_storage import ArtifactUpload, EpisodeCommand, ResearchRecord, ResearchSession
from .scenarios import content_hash
from .storage import Artifact, Episode, WorkLease, utc_iso


def upload_view(row: ArtifactUpload) -> dict[str, Any]:
    return {"id": row.id, "status": row.status, "artifact_id": row.artifact_id,
        "manifest": row.document, "created_at": utc_iso(row.created_at)}


def install_transfer_routes(app: FastAPI, service: ResearchService) -> None:
    router = APIRouter(prefix="/v1")
    repo, settings, store = service.repository, service.settings, app.state.artifact_store
    root = settings.research_upload_root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    def upload(db: Any, owner: str, upload_id: str) -> ArtifactUpload:
        row = db.get(ArtifactUpload, upload_id)
        if row is None or row.owner_id != owner:
            raise ResearchError("upload not found", 404)
        return row

    @router.post("/artifact-uploads", status_code=201)
    def initiate(body: UploadCreate,
                 idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        with repo.db.session() as db:
            owner_id = ADMIN_OWNER_ID
            owner = lock_owner(db, owner_id)
            key = request_key(idempotency_key)
            document = body.model_dump(mode="json")
            prior = db.scalar(select(ArtifactUpload).where(ArtifactUpload.owner_id == owner_id,
                ArtifactUpload.request_key == key))
            if prior:
                if prior.request_hash != content_hash(document):
                    raise ResearchError("upload idempotency key conflict")
                return upload_view(prior)
            if owner.reserved_bytes + body.size_bytes > settings.research_owner_storage_bytes:
                raise ResearchError("owner storage quota exhausted", 429)
            if body.run_id:
                owned_record(db, owner_id, body.run_id, "run")
            row = ArtifactUpload(id=new_id("upload"), owner_id=owner_id,
                request_key=key, request_hash=content_hash(document), document=document)
            db.add(row)
            owner.reserved_bytes += body.size_bytes
            db.flush()
            return upload_view(row)

    @router.get("/artifact-uploads/{upload_id}")
    def status(upload_id: str) -> dict[str, Any]:
        with repo.db.session() as db:
            return upload_view(upload(db, ADMIN_OWNER_ID, upload_id))

    @router.put("/artifact-uploads/{upload_id}/data")
    async def transfer(upload_id: str, request: Request) -> dict[str, Any]:
        owner = ADMIN_OWNER_ID
        with repo.db.session() as db:
            lock_owner(db, owner)
            row = upload(db, owner, upload_id)
            if row.status in {"uploaded", "completed"}:
                return upload_view(row)
            if row.status not in {"pending", "failed"}:
                raise ResearchError("upload is already active or expired")
            row.status = "uploading"
            document = row.document
        path = root / (upload_id + ".partial")
        digest, size = hashlib.sha256(), 0
        try:
            with path.open("wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > document["size_bytes"]:
                        raise ResearchError("upload exceeds declared size", 413)
                    digest.update(chunk)
                    await asyncio.to_thread(output.write, chunk)
                await asyncio.to_thread(output.flush)
            if size != document["size_bytes"] or digest.hexdigest() != document["sha256"]:
                raise ResearchError("upload size or checksum mismatch", 422)
            with repo.db.session() as db:
                lock_owner(db, owner)
                row = upload(db, owner, upload_id)
                if row.status != "uploading":
                    raise ResearchError("upload was cancelled or expired")
                row.status = "uploaded"
                return upload_view(row)
        except BaseException:
            path.unlink(missing_ok=True)
            with repo.db.session() as db:
                lock_owner(db, owner)
                row = upload(db, owner, upload_id)
                if row.status == "uploading":
                    row.status = "failed"
            raise

    @router.post("/artifact-uploads/{upload_id}/complete")
    async def complete(upload_id: str) -> dict[str, Any]:
        owner = ADMIN_OWNER_ID
        with repo.db.session() as db:
            lock_owner(db, owner)
            row = upload(db, owner, upload_id)
            if row.status == "completed":
                return upload_view(row)
            if row.status != "uploaded":
                raise ResearchError("upload is not ready to finalize")
            row.status = "finalizing"
            document = row.document
        path = root / (upload_id + ".partial")
        try:
            stored = await asyncio.to_thread(store.put_file, "research", path)
            if stored.sha256 != document["sha256"] or stored.size_bytes != document["size_bytes"]:
                raise ResearchError("uploaded object changed before finalization")
            with repo.db.session() as db:
                lock_owner(db, owner)
                row = upload(db, owner, upload_id)
                if row.status != "finalizing":
                    raise ResearchError("upload was cancelled or expired")
                artifact = Artifact(id=stored.artifact_id, kind="research", uri=stored.uri,
                    sha256=stored.sha256, size_bytes=stored.size_bytes,
                    metadata_json={"owner_id": owner, "upload_id": row.id, **document})
                db.add(artifact)
                db.flush()
                row.artifact_id, row.status = artifact.id, "completed"
                result = upload_view(row)
            path.unlink(missing_ok=True)
            return result
        except BaseException:
            with repo.db.session() as db:
                lock_owner(db, owner)
                row = upload(db, owner, upload_id)
                if row.status == "finalizing":
                    row.status = "uploaded" if path.exists() else "failed"
            raise

    @router.post("/artifact-uploads/{upload_id}/cancel")
    def cancel_upload(upload_id: str) -> dict[str, Any]:
        with repo.db.session() as db:
            owner_id = ADMIN_OWNER_ID
            owner = lock_owner(db, owner_id)
            row = upload(db, owner_id, upload_id)
            if row.status == "completed":
                raise ResearchError("completed artifacts are immutable")
            if row.status not in {"cancelled", "expired"}:
                owner.reserved_bytes -= row.document["size_bytes"]
                row.status = "cancelled"
            (root / (row.id + ".partial")).unlink(missing_ok=True)
            return upload_view(row)

    @router.post("/dataset-snapshots", status_code=202)
    def snapshot(body: DatasetCreate,
                 idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        with repo.db.session() as db:
            owner = ADMIN_OWNER_ID
            lock_owner(db, owner)
            for run_id in body.run_ids:
                owned_record(db, owner, run_id, "run")
            key = request_key(idempotency_key)
            prior = db.scalar(select(ResearchRecord).where(ResearchRecord.owner_id == owner,
                ResearchRecord.kind == "dataset", ResearchRecord.request_key == key))
            if prior:
                # The selection is frozen on first submission, even if new sessions arrive.
                if any(prior.document.get(k) != v for k, v in body.model_dump(mode="json").items()):
                    raise ResearchError("dataset idempotency key conflict")
                return record_view(prior)
            sessions = list(db.scalars(select(ResearchSession).where(ResearchSession.owner_id == owner,
                ResearchSession.run_id.in_(body.run_ids))))
            if body.split == "train" and any(row.document["split"] == "test" for row in sessions):
                raise ResearchError("train export rejected: selected runs contain test episodes", 422)
            selected = [row for row in sessions if row.document["split"] == body.split and
                        (not body.families or row.document["family"] in body.families)]
            document = {**body.model_dump(mode="json"), "session_ids": [r.id for r in selected]}
            row = service.put_record(db, owner, "dataset", key,
                {**document, "cutoff": datetime.now(UTC).isoformat(), "schema_version": "aml.dataset.v1"})
            db.add(WorkLease(id=new_id("job"), job_type="research_dataset", payload={"dataset_id": row.id}))
            return record_view(row)

    @router.get("/dataset-snapshots")
    def snapshots() -> dict[str, Any]:
        with repo.db.session() as db:
            return {"items": [record_view(row) for row in db.scalars(select(ResearchRecord).where(
                ResearchRecord.owner_id == ADMIN_OWNER_ID, ResearchRecord.kind == "dataset")
                .order_by(ResearchRecord.created_at.desc()).limit(500))]}

    @router.get("/dataset-snapshots/{dataset_id}")
    def snapshot_detail(dataset_id: str) -> dict[str, Any]:
        with repo.db.session() as db:
            owner = ADMIN_OWNER_ID
            result = record_view(owned_record(db, owner, dataset_id, "dataset"))
            completed = db.scalar(select(ResearchRecord).where(ResearchRecord.owner_id == owner,
                ResearchRecord.kind == "dataset_result", ResearchRecord.request_key == dataset_id))
            result["status"] = "completed" if completed else "pending"
            result["manifest"] = completed.document if completed else None
            if not completed:
                job = next((j for j in db.scalars(select(WorkLease).where(WorkLease.job_type == "research_dataset")) if j.payload["dataset_id"] == dataset_id), None)
                if job and job.status == "FAILED":
                    result["status"] = "failed"
            return result

    app.include_router(router)


class DatasetRunner:
    def __init__(self, service: ResearchService, store: Any):
        self.service, self.store = service, store

    async def run(self, dataset_id: str) -> None:
        await asyncio.to_thread(self.build, dataset_id)

    def build(self, dataset_id: str) -> None:
        repo = self.service.repository
        with repo.db.session() as db:
            dataset = db.get(ResearchRecord, dataset_id)
            assert dataset is not None
            if db.scalar(select(ResearchRecord.id).where(ResearchRecord.kind == "dataset_result", ResearchRecord.request_key == dataset_id)):
                return
            config = dataset.document
            cutoff = datetime.fromisoformat(config["cutoff"])
            # Freeze exact operation/generation documents into a database-backed manifest
            # before writing an artifact, so a retry cannot include newly completed steps.
            frozen = db.scalar(select(ResearchRecord).where(ResearchRecord.kind == "dataset_freeze", ResearchRecord.request_key == dataset_id))
            if frozen is None:
                lock_owner(db, dataset.owner_id)
                sessions = {row.id: row for row in db.scalars(select(ResearchSession).where(ResearchSession.id.in_(config["session_ids"])))}
                generations = [row for row in db.scalars(select(ResearchRecord).where(
                    ResearchRecord.kind == "generation", ResearchRecord.owner_id == dataset.owner_id,
                    ResearchRecord.created_at <= cutoff)) if row.document.get("session_id") in sessions]
                by_id = {g.id: g for g in generations}
                records: list[dict[str, Any]] = []
                used: set[str] = set()
                for command in db.scalars(select(EpisodeCommand).where(EpisodeCommand.session_id.in_(sessions),
                    EpisodeCommand.created_at <= cutoff).order_by(EpisodeCommand.created_at, EpisodeCommand.id)):
                    session = sessions[command.session_id]
                    generation_id = command.payload.get("generation_id")
                    gen = by_id.get(generation_id) if generation_id else None
                    if gen:
                        used.add(gen.id)
                    output = command.result if command.completed_at and command.completed_at.replace(tzinfo=UTC) <= cutoff else None
                    episode_id = (output or {}).get("episode_id", command.payload.get("episode_id"))
                    episode = db.get(Episode, episode_id) if episode_id else None
                    lineage = {"operation_id": command.id, "session_id": session.id, "run_id": session.run_id,
                        "episode_id": episode_id,
                        "target_version_id": session.document["target_version_id"],
                        "scenario_version_id": session.document["scenario_version_id"],
                        "verifier_version": session.document["verifier_version"],
                        "baseline_reward_version": session.document["baseline_reward_version"],
                        "split": session.document["split"], "family": session.document["family"],
                        "seed": episode.seed if episode else (output or {}).get("seed", session.document["seed"]), "group_id": session.document["group_id"]}
                    generated = gen.document if gen else None
                    record = {"schema_version": "aml.dataset.v1", "lineage": lineage,
                        "status": command.status if output else "partial_at_snapshot", "kind": command.kind,
                        "provenance": command.payload.get("provenance"), "generation_id": generation_id,
                        "action": command.payload.get("action"),
                        "public_observation": (output or {}).get("public_observation"),
                        "generation": generated}
                    if config["view"] == "research":
                        record["research"] = {k: (output or {}).get(k) for k in ("outcome", "measurements", "baseline_reward", "usage")}
                        record["reported_rewards"] = [r.document for r in db.scalars(select(ResearchRecord).where(
                            ResearchRecord.owner_id == dataset.owner_id, ResearchRecord.kind == "reward_annotation",
                            ResearchRecord.created_at <= cutoff)) if r.document["operation_id"] == command.id]
                    records.append(record)
                for gen in generations:
                    if gen.id not in used:
                        records.append({"schema_version": "aml.dataset.v1", "kind": "unexecuted_generation",
                            "lineage": {"run_id": gen.document.get("run_id"), "session_id": gen.document["session_id"],
                                "episode_id": gen.document["episode_id"], "source_record_id": gen.id},
                            "status": "unexecuted", "generation_id": gen.id, "generation": gen.document})
                frozen = self.service.put_record(db, dataset.owner_id, "dataset_freeze", dataset_id, {"records": records})
            records = frozen.document["records"]
        with tempfile.TemporaryDirectory(prefix="aml-dataset-") as directory:
            path = Path(directory) / ("records." + config["format"])
            if config["format"] == "jsonl":
                with path.open("wb") as output:
                    for record in records:
                        output.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n")
            else:
                import pyarrow as pa
                import pyarrow.parquet as pq
                # Raw JSON column preserves opaque trainer data, nulls and heterogeneous actions.
                table = pa.table({"schema_version": ["aml.dataset.v1"] * len(records),
                    "record_json": [json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records]})
                pq.write_table(table, path, compression="zstd")
            stored = self.store.put_file("datasets", path, "." + config["format"])
        with repo.db.session() as db:
            owner = lock_owner(db, dataset.owner_id)
            if db.scalar(select(ResearchRecord.id).where(ResearchRecord.kind == "dataset_result", ResearchRecord.request_key == dataset_id)):
                return
            if owner.reserved_bytes + stored.size_bytes > self.service.settings.research_owner_storage_bytes:
                raise ResearchError("dataset exceeds storage quota")
            owner.reserved_bytes += stored.size_bytes
            db.add(Artifact(id=stored.artifact_id, kind="dataset", uri=stored.uri, sha256=stored.sha256,
                size_bytes=stored.size_bytes, metadata_json={"owner_id": dataset.owner_id, "dataset_id": dataset_id}))
            db.flush()
            self.service.put_record(db, dataset.owner_id, "dataset_result", dataset_id,
                {"schema_version": "aml.dataset.v1", "artifact_id": stored.artifact_id,
                    "sha256": stored.sha256, "size_bytes": stored.size_bytes, "record_count": len(records),
                    "filters": config, "freeze_hash": frozen.content_hash})

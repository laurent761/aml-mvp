from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .contracts import new_id


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: str
    uri: str
    sha256: str
    size_bytes: int


class ArtifactStore(Protocol):
    def put_bytes(self, kind: str, data: bytes, suffix: str = ".bin") -> StoredArtifact: ...
    def put_json(self, kind: str, document: dict[str, Any]) -> StoredArtifact: ...
    def get_bytes(self, uri: str) -> bytes: ...
    def put_file(self, kind: str, path: Path, suffix: str = ".bin") -> StoredArtifact: ...
    def iter_bytes(self, uri: str) -> Iterator[bytes]: ...


class LocalArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, kind: str, data: bytes, suffix: str = ".bin") -> StoredArtifact:
        artifact_id = new_id("artifact")
        directory = self.root / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / f"{artifact_id}{suffix}").resolve()
        if self.root not in path.parents:
            raise ValueError("artifact path escaped storage root")
        path.write_bytes(data)
        return StoredArtifact(
            artifact_id=artifact_id,
            uri=f"file://{path}",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )

    def put_json(self, kind: str, document: dict[str, Any]) -> StoredArtifact:
        data = json.dumps(document, sort_keys=True, indent=2).encode()
        return self.put_bytes(kind, data, ".json")

    def put_file(self, kind: str, path: Path, suffix: str = ".bin") -> StoredArtifact:
        artifact_id = new_id("artifact")
        target = (self.root / kind / f"{artifact_id}{suffix}").resolve()
        if self.root not in target.parents:
            raise ValueError("artifact path escaped storage root")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".partial")
        digest, size = hashlib.sha256(), 0
        try:
            with path.open("rb") as source, temporary.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredArtifact(artifact_id, f"file://{target}", digest.hexdigest(), size)

    def iter_bytes(self, uri: str) -> Iterator[bytes]:
        if not uri.startswith("file://"):
            raise ValueError("unsupported artifact URI")
        path = Path(uri.removeprefix("file://")).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact URI escaped storage root")
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    def get_bytes(self, uri: str) -> bytes:
        if not uri.startswith("file://"):
            raise ValueError("unsupported artifact URI")
        path = Path(uri.removeprefix("file://")).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact URI escaped storage root")
        return path.read_bytes()


class S3ArtifactStore:
    """Optional S3/MinIO adapter loaded only when the `s3` extra is installed."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client=None,
    ):
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("install the s3 extra to use S3ArtifactStore") from exc
            client = boto3.client(
                "s3", endpoint_url=endpoint_url, region_name=region_name
            )
        self.bucket, self.client = bucket, client

    def put_bytes(self, kind: str, data: bytes, suffix: str = ".bin") -> StoredArtifact:
        artifact_id = new_id("artifact")
        key = f"{kind}/{artifact_id}{suffix}"
        digest = hashlib.sha256(data).hexdigest()
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            Metadata={"sha256": digest},
        )
        return StoredArtifact(
            artifact_id=artifact_id,
            uri=f"s3://{self.bucket}/{key}",
            sha256=digest,
            size_bytes=len(data),
        )

    def get_bytes(self, uri: str) -> bytes:
        prefix = f"s3://{self.bucket}/"
        if not uri.startswith(prefix):
            raise ValueError("artifact URI does not belong to configured bucket")
        return self.client.get_object(Bucket=self.bucket, Key=uri.removeprefix(prefix))["Body"].read()

    def put_file(self, kind: str, path: Path, suffix: str = ".bin") -> StoredArtifact:
        artifact_id = new_id("artifact")
        key = f"{kind}/{artifact_id}{suffix}"
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        # boto3's multipart transfer bounds memory and aborts failed multipart uploads.
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs={"Metadata": {"sha256": digest.hexdigest()}})
        return StoredArtifact(artifact_id, f"s3://{self.bucket}/{key}", digest.hexdigest(), path.stat().st_size)

    def iter_bytes(self, uri: str) -> Iterator[bytes]:
        prefix = f"s3://{self.bucket}/"
        if not uri.startswith(prefix):
            raise ValueError("artifact URI does not belong to configured bucket")
        body = self.client.get_object(Bucket=self.bucket, Key=uri.removeprefix(prefix))["Body"]
        try:
            yield from body.iter_chunks(chunk_size=1024 * 1024)
        finally:
            body.close()

    def put_json(self, kind: str, document: dict[str, Any]) -> StoredArtifact:
        data = json.dumps(document, sort_keys=True, indent=2).encode()
        return self.put_bytes(kind, data, ".json")


def create_artifact_store(
    *,
    backend: str,
    local_root: Path,
    bucket: str | None = None,
    endpoint_url: str | None = None,
    region_name: str | None = None,
    client: Any = None,
) -> ArtifactStore:
    selected = backend.strip().lower()
    if selected == "local":
        return LocalArtifactStore(local_root)
    if selected in {"s3", "minio"}:
        if not bucket:
            raise ValueError("ARTIFACT_BUCKET is required for S3/MinIO artifact storage")
        return S3ArtifactStore(
            bucket, endpoint_url=endpoint_url, region_name=region_name, client=client
        )
    raise ValueError(f"unsupported artifact backend: {backend}")


def artifact_store_from_environment(local_root: Path) -> ArtifactStore:
    return create_artifact_store(
        backend=os.environ.get("ARTIFACT_BACKEND", "local"),
        local_root=local_root,
        bucket=os.environ.get("ARTIFACT_BUCKET"),
        endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
        region_name=os.environ.get("S3_REGION"),
    )


def verify_artifact(data: bytes, expected_sha256: str, expected_size: int) -> bool:
    return len(data) == expected_size and hashlib.sha256(data).hexdigest() == expected_sha256

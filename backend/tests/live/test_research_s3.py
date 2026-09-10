"""Optional multipart/streaming gate against a disposable S3-compatible service."""
import hashlib
import os
import uuid

import pytest

from adversarial_agent_mvp.artifacts import S3ArtifactStore


def test_multipart_checkpoint_round_trip(tmp_path, monkeypatch):
    endpoint = os.getenv("MVP_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("set MVP_TEST_S3_ENDPOINT and MVP_TEST_S3_ACCESS_KEY/SECRET_KEY for disposable S3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", os.environ["MVP_TEST_S3_ACCESS_KEY"])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", os.environ["MVP_TEST_S3_SECRET_KEY"])
    bucket = "research-validation-" + uuid.uuid4().hex
    store = S3ArtifactStore(bucket, endpoint_url=endpoint)
    store.client.create_bucket(Bucket=bucket)
    key = None
    try:
        path = tmp_path / "checkpoint.bin"
        digest = hashlib.sha256()
        with path.open("wb") as output:
            for _ in range(24):
                chunk = os.urandom(1024 * 1024)
                output.write(chunk)
                digest.update(chunk)
        stored = store.put_file("checkpoints", path)
        key = stored.uri.removeprefix(f"s3://{bucket}/")
        assert stored.size_bytes == path.stat().st_size
        assert stored.sha256 == digest.hexdigest()
        head = store.client.head_object(Bucket=bucket, Key=key)
        assert "-" in head["ETag"]  # Actual multipart upload, not whole-object buffering.
        downloaded = hashlib.sha256()
        size = 0
        for chunk in store.iter_bytes(stored.uri):
            assert len(chunk) <= 1024 * 1024
            downloaded.update(chunk)
            size += len(chunk)
        assert size == stored.size_bytes and downloaded.hexdigest() == stored.sha256
    finally:
        if key:
            store.client.delete_object(Bucket=bucket, Key=key)
        store.client.delete_bucket(Bucket=bucket)

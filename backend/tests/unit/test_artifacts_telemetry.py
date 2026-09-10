import asyncio
import io
import json

import httpx
import pytest

from adversarial_agent_mvp.artifacts import S3ArtifactStore, verify_artifact
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.telemetry import (
    MlflowTracker,
    MlflowTrackingError,
    build_mlflow_campaign_tracker,
    build_mlflow_evaluation_hook,
)


class S3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, Metadata):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def test_s3_compatible_artifact_round_trip():
    store = S3ArtifactStore("evidence", client=S3Client())
    artifact = store.put_bytes("campaign", b"proof")
    assert store.get_bytes(artifact.uri) == b"proof"
    assert verify_artifact(b"proof", artifact.sha256, artifact.size_bytes)


def test_mlflow_adapter_hashes_secret_like_parameters():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if request.url.path.endswith("create"):
            return httpx.Response(200, json={"run": {"info": {"run_id": "r1"}}})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tracker = MlflowTracker("http://mlflow", client)
    run_id = tracker.create_run("1", "campaign")
    tracker.log_batch(run_id, metrics={"reward": 1.0}, params={"api_key": "secret"})
    assert requests[1]["params"][0]["value"].startswith("sha256:")


def test_mlflow_adapter_creates_experiment_and_terminates_run():
    requests = []

    def handler(request):
        requests.append((request.url.path, json.loads(request.content or b"{}")))
        if request.method == "GET":
            return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
        if request.url.path.endswith("experiments/create"):
            return httpx.Response(200, json={"experiment_id": "experiment-7"})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tracker = MlflowTracker("http://mlflow", client)
    assert tracker.get_or_create_experiment("campaigns") == "experiment-7"
    tracker.terminate_run("run-1")

    assert requests[-1][1]["run_id"] == "run-1"
    assert requests[-1][1]["status"] == "FINISHED"


@pytest.mark.asyncio
async def test_campaign_tracker_records_only_config_hash_and_finite_metrics():
    requests = []

    def handler(request):
        body = json.loads(request.content or b"{}")
        requests.append((request.url.path, body))
        if request.url.path.endswith("get-by-name"):
            return httpx.Response(200, json={"experiment": {"experiment_id": "1"}})
        if request.url.path.endswith("runs/create"):
            return httpx.Response(200, json={"run": {"info": {"run_id": "run-1"}}})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw_tracker = MlflowTracker("http://mlflow", client)
    settings = Settings(mlflow_tracking_uri="http://mlflow")
    tracker = build_mlflow_campaign_tracker(settings, tracker=raw_tracker)
    assert tracker is not None

    await asyncio.gather(
        tracker.record_campaign(
            campaign_id="campaign-1",
            config={"schema_version": "v1", "api_key": "never-log-this"},
            metrics={
                "reward": 1.25,
                "episodes": 2,
                "cancelled": False,
                "invalid": float("nan"),
                "label": "ignored",
            },
        ),
        tracker.record_campaign(
            campaign_id="campaign-2",
            config={"schema_version": "v1", "api_key": "never-log-this-either"},
            metrics={"reward": 0.5},
        ),
    )

    serialized = json.dumps(requests)
    assert "never-log-this" not in serialized
    assert sum(path.endswith("get-by-name") for path, _ in requests) == 1
    log_batch = next(
        body
        for path, body in requests
        if path.endswith("runs/log-batch")
        and any(metric["key"] == "episodes" for metric in body["metrics"])
    )
    assert {metric["key"] for metric in log_batch["metrics"]} == {"reward", "episodes"}
    params = {param["key"]: param["value"] for param in log_batch["params"]}
    assert len(params["config_sha256"]) == 64
    assert params["config_schema"] == "v1"
    update = next(body for path, body in requests if path.endswith("runs/update"))
    assert update["status"] == "FINISHED"


@pytest.mark.asyncio
async def test_campaign_tracker_marks_started_run_failed_with_safe_error():
    updates = []

    def handler(request):
        if request.url.path.endswith("get-by-name"):
            return httpx.Response(200, json={"experiment": {"experiment_id": "1"}})
        if request.url.path.endswith("runs/create"):
            return httpx.Response(200, json={"run": {"info": {"run_id": "run-1"}}})
        if request.url.path.endswith("runs/log-batch"):
            return httpx.Response(500, text="internal token=never-expose")
        if request.url.path.endswith("runs/update"):
            updates.append(json.loads(request.content))
            return httpx.Response(200, json={})
        raise AssertionError(request.url.path)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw_tracker = MlflowTracker("http://mlflow", client)
    tracker = build_mlflow_campaign_tracker(
        Settings(mlflow_tracking_uri="http://mlflow"),
        tracker=raw_tracker,
    )
    assert tracker is not None

    with pytest.raises(MlflowTrackingError) as raised:
        await tracker.record_campaign(campaign_id="campaign-1", config={}, metrics={})

    assert str(raised.value) == "MLflow campaign recording failed"
    assert "never-expose" not in str(raised.value)
    assert updates[-1]["status"] == "FAILED"


def test_mlflow_factories_are_optional_and_evaluation_hook_is_resolved():
    disabled = Settings(mlflow_tracking_uri=None)
    assert build_mlflow_campaign_tracker(disabled) is None
    assert build_mlflow_evaluation_hook(disabled) is None

    def handler(request):
        assert request.url.path.endswith("get-by-name")
        return httpx.Response(200, json={"experiment": {"experiment_id": "experiment-1"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw_tracker = MlflowTracker("http://mlflow", client)
    enabled = Settings(mlflow_tracking_uri="http://mlflow")
    hook = build_mlflow_evaluation_hook(enabled, tracker=raw_tracker)
    assert hook is not None
    assert hook.tracker is raw_tracker
    assert hook.experiment_id == "experiment-1"

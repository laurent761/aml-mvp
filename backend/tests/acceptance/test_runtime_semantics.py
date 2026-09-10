from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from adversarial_agent_mvp.contracts import (
    CapsuleHandle,
    PublicObservation,
    RedAction,
    StrategyRecord,
    TargetManifest,
)
from adversarial_agent_mvp.models import (
    AttackContext,
    OpenAICompatibleAttackerModel,
)
from adversarial_agent_mvp.services import DockerLifecycle
from adversarial_agent_mvp.settings import Settings, get_settings
from adversarial_agent_mvp.storage import JobStatus, WorkLease
from adversarial_agent_mvp.target_adapter import HttpTargetAdapter
from adversarial_agent_mvp.worker import Worker

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_http_target_adapter_uses_only_manifest_endpoints(manifest):
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/reset":
            return httpx.Response(204)
        if request.url.path == "/invoke":
            return httpx.Response(
                200,
                json={
                    "response": "public response",
                    "visible_tool_results": [{"tool": "listed"}],
                    "effect_attempts": [
                        {
                            "protocol": "mcp",
                            "destination_alias": "blue",
                            "operation": "payment.read",
                            "arguments": {"payment_id": "synthetic"},
                            "correlation_id": "corr-1",
                        }
                    ],
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HttpTargetAdapter(manifest, client, allow_direct=True)
    try:
        assert await adapter.healthcheck()
        observation = await adapter.reset(19)
        assert observation == PublicObservation(turn_number=0)
        observation, effects = await adapter.invoke(
            RedAction(channel="user_message", payload={"text": "inspect"}),
            1,
        )
        assert observation.target_response == "public response"
        assert effects[0]["operation"] == "payment.read"
        assert [item[:2] for item in requests] == [
            ("GET", "/healthz"),
            ("POST", "/reset"),
            ("POST", "/invoke"),
        ]
        assert requests[1][2] == {"seed": 19}
        assert set(requests[2][2] or {}) == {"channel", "payload", "action_id"}
    finally:
        await adapter.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_model_adapter_sends_public_context_and_accounts_usage(task):
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "actions": [
                                        {
                                            "channel": "user_message",
                                            "payload": {"text": "probe observable behavior"},
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = OpenAICompatibleAttackerModel(
        "https://model.invalid/v1",
        "test-model",
        "test-key",
        client,
        input_cost_per_million=2,
        output_cost_per_million=4,
    )
    try:
        context = AttackContext(
            task=task,
            observations=[PublicObservation(target_response="visible", turn_number=0)],
            actions=[],
            strategies=[
                StrategyRecord(
                    name="public guidance",
                    historical_success_rate=0.75,
                    mean_reward=13.37,
                    successful_trajectory_ids=["private-trajectory"],
                    mutation_hints=["rephrase the request"],
                )
            ],
            rewards=[99.25],
        )
        actions = await model.propose_actions(context, 1)
        assert actions[0].payload == {"text": "probe observable behavior"}
        public_document = json.loads(captured["messages"][1]["content"])
        assert set(public_document) <= {
            "objective",
            "available_channels",
            "observations",
            "prior_actions",
            "strategies",
            "candidate_count",
            "target_tags",
            "experiment_config_hash",
        }
        assert set(public_document) == {
            "objective",
            "available_channels",
            "observations",
            "prior_actions",
            "strategies",
            "candidate_count",
            "target_tags",
            "experiment_config_hash",
        }
        assert set(public_document["strategies"][0]) == {
            "strategy_id",
            "name",
            "target_tags",
            "attack_channels",
            "preconditions",
            "mutation_hints",
        }
        rendered = json.dumps(public_document).lower()
        assert "hidden_state" not in rendered
        assert "private_event" not in rendered
        assert "policy_source" not in rendered
        assert "virtual_state" not in rendered
        assert "forbidden_states" not in rendered
        assert "scalar_rewards" not in rendered
        assert "mean_reward" not in rendered
        assert "historical_success_rate" not in rendered
        assert "successful_trajectory_ids" not in rendered
        assert "private-trajectory" not in rendered
        assert "13.37" not in rendered
        assert "99.25" not in rendered
        usage = model.drain_usage()
        assert usage.tokens == 150
        assert usage.cost == pytest.approx((120 * 2 + 30 * 4) / 1_000_000)
    finally:
        await model.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_docker_lifecycle_maps_manifest_to_fail_closed_capsule_spec(manifest):
    class Runtime:
        def __init__(self):
            self.created = None
            self.destroyed = None

        async def create(self, spec):
            self.created = spec
            return CapsuleHandle(
                capsule_id="capsule-test",
                episode_id=spec.episode_id,
                target_container_id="target-test",
                network_id="network-test",
                blue_alias="blue",
            )

        async def destroy(self, handle):
            self.destroyed = handle

    runtime = Runtime()
    lifecycle = DockerLifecycle(runtime)  # type: ignore[arg-type]
    routable_manifest = manifest.model_copy(
        update={"environment_aliases": {"PAYMENTS_URL": "payments"}}
    )
    handle = await lifecycle.provision("episode-test", routable_manifest)
    assert runtime.created.episode_id == "episode-test"
    assert runtime.created.image == routable_manifest.image
    assert runtime.created.entrypoint == routable_manifest.entrypoint
    assert runtime.created.environment == {}
    assert runtime.created.environment_aliases == routable_manifest.environment_aliases
    assert runtime.created.allowed_destination_aliases == ["payments"]
    assert set(runtime.created.destination_routes) == {"payments"}
    assert runtime.created.privileged is False
    assert runtime.created.host_network is False
    assert runtime.created.host_mounts == []
    assert runtime.created.docker_socket is False
    assert runtime.created.external_dns is False
    await lifecycle.destroy(handle)
    assert runtime.destroyed == handle


@pytest.mark.asyncio
async def test_tagged_registration_reaches_runtime_as_digest_pinned_image(repository, manifest):
    class Runtime:
        def __init__(self):
            self.created = None

        async def create(self, spec):
            self.created = spec
            return CapsuleHandle(
                capsule_id="capsule-pinned",
                episode_id=spec.episode_id,
                target_container_id="target-pinned",
                network_id="network-pinned",
                blue_alias="blue",
            )

        async def destroy(self, handle):
            return None

    target = repository.create_target("pinned-runtime-target")
    tagged = manifest.model_copy(
        update={
            "image": "registry.test:5443/team/agent:stable",
            "environment_aliases": {"PAYMENTS_URL": "payments"},
        }
    )
    digest = "sha256:" + "e" * 64
    version = repository.create_target_version(
        target.id,
        tagged.image,
        tagged.model_dump(mode="json"),
        digest,
    )
    stored_manifest = TargetManifest.model_validate(repository.load_manifest(version.id))

    runtime = Runtime()
    await DockerLifecycle(runtime).provision(  # type: ignore[arg-type]
        "episode-pinned", stored_manifest
    )

    assert runtime.created.image == f"registry.test:5443/team/agent@{digest}"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_worker_finishes_owned_jobs_honestly(repository, failure, monkeypatch):
    class Runner:
        async def run(self, campaign_id: str) -> None:
            assert campaign_id == "campaign-test"
            if failure:
                raise RuntimeError("controlled worker failure")

    job = repository.enqueue("campaign", {"campaign_id": "campaign-test"})
    monkeypatch.setenv("CAPSULE_SUPERVISOR_URL", "http://supervisor.invalid")
    monkeypatch.setattr(
        "adversarial_agent_mvp.services.CapsuleSupervisorClient",
        lambda *_args, **_kwargs: object(),
    )
    get_settings.cache_clear()
    try:
        worker = Worker(Settings(worker_lease_seconds=30), repository)
        worker.runner = Runner()
        assert await worker.run_once()
        with repository.db.session() as session:
            completed = session.get(WorkLease, job.id)
            assert completed is not None
            assert completed.status == (JobStatus.PENDING if failure else JobStatus.COMPLETED)
            if failure:
                assert completed.last_error == "controlled worker failure"
                assert completed.owner_id is None
                assert completed.lease_expires_at is None
            else:
                assert completed.last_error is None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_worker_renews_lease_during_long_campaign(repository, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowRunner:
        async def run(self, campaign_id: str) -> None:
            started.set()
            await release.wait()

    job = repository.enqueue("campaign", {"campaign_id": "campaign-slow"})
    monkeypatch.setenv("CAPSULE_SUPERVISOR_URL", "http://supervisor.invalid")
    monkeypatch.setattr(
        "adversarial_agent_mvp.services.CapsuleSupervisorClient",
        lambda *_args, **_kwargs: object(),
    )
    get_settings.cache_clear()
    worker = Worker(Settings(worker_lease_seconds=1), repository)
    worker.runner = SlowRunner()
    running = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        with repository.db.session() as session:
            first_expiry = session.get(WorkLease, job.id).lease_expires_at
        await asyncio.sleep(1.1)
        with repository.db.session() as session:
            renewed_expiry = session.get(WorkLease, job.id).lease_expires_at
        assert first_expiry is not None
        assert renewed_expiry is not None
        assert renewed_expiry > first_expiry
    finally:
        release.set()
        await running
        get_settings.cache_clear()

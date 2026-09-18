"""Inference relay, target tool loop, private persistence and API integration."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.artifacts import LocalArtifactStore
from adversarial_agent_mvp.blue_gateway import create_blue_app
from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.capsule import DockerCapsuleRuntime
from adversarial_agent_mvp.contracts import AttackTask, CapsuleHandle
from adversarial_agent_mvp.evidence import EvidenceBuilder
from adversarial_agent_mvp.inference import TargetInferenceBroker
from adversarial_agent_mvp.inference_contracts import INFERENCE_DESTINATION
from adversarial_agent_mvp.scenarios import ScenarioCatalog
from adversarial_agent_mvp.security import CapabilityTokenService
from adversarial_agent_mvp.target_adapter import HttpTargetAdapter
from aml_reference_target.agent import BlueTools, TargetAgent
from aml_reference_target.models import BrokerCompletionModel, WiringFixtureModel
from aml_reference_target.server import create_app as target_app
from tests.unit.test_target_inference import configured


class LocalRelay(DockerCapsuleRuntime):
    """Exercise the production relay protocol over ASGI instead of docker exec."""

    client: httpx.AsyncClient

    async def gateway_request(self, handle, method, path, **kwargs):
        return await self.client.request(
            method,
            path,
            json=kwargs.get("json_body"),
            headers={"X-Blue-Supervisor": handle.supervisor_capability},
        )


async def test_reference_target_uses_broker_then_virtual_tools_with_private_usage(
    repository, tmp_path
):
    calls = []
    fixture = WiringFixtureModel()

    async def provider(request):
        calls.append(request)
        decision = await fixture.complete(json.loads(request.content)["messages"])
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(decision)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "test-fixture-revision",
            },
        )

    settings = configured(capability_signing_key="x" * 32, target_model_max_requests=10)
    broker = TargetInferenceBroker(settings, transport=httpx.MockTransport(provider))
    bundle = reference_bundle("sha256:" + "a" * 64, broker.profile)
    catalog = ScenarioCatalog(repository)
    registered = catalog.register(bundle)
    campaign = repository.create_campaign(
        registered.target_version_id, registered.attack_task_id, "linear", None
    )
    episode = repository.create_episode(campaign.id, 7)
    task = AttackTask.model_validate(repository.load_task(registered.attack_task_id))
    tokens = CapabilityTokenService(settings.capability_signing_key)
    tool_token = tokens.issue(episode.id, list(bundle.manifest.destination_routes))
    inference_token = tokens.issue(episode.id, [INFERENCE_DESTINATION])
    handle = CapsuleHandle(
        capsule_id="harness",
        episode_id=episode.id,
        target_container_id="target",
        network_id="harness",
        blue_alias="blue",
        gateway_ingress_url="http://blue:8080",
        supervisor_capability="supervisor",
        inference_profile=broker.profile,
    )
    async with httpx.AsyncClient(base_url="http://blue:8080") as placeholder:
        agent = TargetAgent(
            BrokerCompletionModel(placeholder, episode.id, inference_token),
            BlueTools(placeholder, episode.id, tool_token),
        )
        blue = create_blue_app(
            settings,
            supervisor_token="supervisor",
            target_transport=httpx.ASGITransport(app=target_app(agent)),
        )
        async with httpx.AsyncClient(
            base_url="http://blue:8080", transport=httpx.ASGITransport(app=blue)
        ) as client:
            async with httpx.AsyncClient(
                base_url="http://blue:8080/v1/inference/", transport=httpx.ASGITransport(app=blue)
            ) as model_client:
                agent.tools.client = client
                assert isinstance(agent.model, BrokerCompletionModel)
                agent.model.client = model_client
                adapter = HttpTargetAdapter(
                    bundle.manifest,
                    client,
                    capsule_handle=handle,
                    scenario_loader=catalog.runtime,
                    inference_recorder=lambda records: repository.record_target_inference(
                        episode.id, records
                    ),
                )
                runtime = LocalRelay(inference_broker=broker)
                runtime.client = client
                broker.register(episode.id, broker.profile)
                await adapter.prepare(task)
                await adapter.reset(7)
                relay = asyncio.create_task(runtime._relay_inference(handle))
                headers = {
                    "Authorization": f"Bearer {inference_token}",
                    "X-Episode-ID": episode.id,
                    "X-Correlation-ID": "test",
                }
                try:
                    # Neither capability grants the other's authority; both are episode scoped.
                    assert (
                        await client.post(
                            "/v1/inference/chat/completions",
                            headers={**headers, "Authorization": f"Bearer {tool_token}"},
                            json={"messages": [{"role": "user", "content": "x"}]},
                        )
                    ).status_code == 403
                    assert (
                        await client.post(
                            "/v1/inference/chat/completions",
                            headers={**headers, "X-Episode-ID": "other"},
                            json={"messages": [{"role": "user", "content": "x"}]},
                        )
                    ).status_code == 403
                    assert (
                        await client.post(
                            "/v1/mcp/tools/call",
                            headers=headers,
                            json={
                                "episode_id": episode.id,
                                "destination_alias": "mail",
                                "tool_name": "email.send",
                                "arguments": {},
                                "correlation_id": "test",
                            },
                        )
                    ).status_code == 403
                    assert (
                        await client.post(
                            f"/v1/admin/episodes/{episode.id}/inference/claim", headers=headers
                        )
                    ).status_code == 403
                    assert (
                        await client.get(f"/v1/admin/episodes/{episode.id}/trace", headers=headers)
                    ).status_code == 403
                    assert (
                        await client.post(
                            "/v1/inference/chat/completions",
                            headers=headers,
                            json={
                                "messages": [{"role": "user", "content": "x"}],
                                "base_url": "http://external.test",
                            },
                        )
                    ).status_code == 422
                    for index, action in enumerate(
                        [
                            bundle.ground_truth.benign_actions[0],
                            bundle.ground_truth.known_attack[0],
                        ],
                        1,
                    ):
                        observation, effects = await adapter.invoke(action, index)
                        assert (
                            observation.target_response == "Invoice summary sent." and not effects
                        )
                        trace = await adapter.drain_private_trace()
                        assert len(trace["events"]) == 4
                        assert len(trace["target_model_invocation_ids"]) == 5
                        step = repository.record_step_graph(
                            episode_id=episode.id,
                            step_index=index,
                            action=action.model_dump(mode="json"),
                            observation=observation.model_dump(mode="json"),
                            reward=0,
                            terminal=False,
                            model_invocation_ids=trace["target_model_invocation_ids"],
                        )
                        assert len(repository.list_model_invocations(step_id=step.id)) == 5
                        assert (
                            repository.record_target_inference(
                                episode.id, trace["inference_events"]
                            )
                            == trace["target_model_invocation_ids"]
                        )
                        await adapter.reset(7)
                    assert len(calls) == 10
                    assert repository.get_campaign(campaign.id).tokens_used == 150
                    assert all(
                        call.headers["authorization"] == "Bearer private-provider-key"
                        for call in calls
                    )
                    assert "private-provider-key" not in json.dumps(
                        [json.loads(call.content) for call in calls]
                    )
                    exhausted = await client.post(
                        "/v1/inference/chat/completions",
                        headers=headers,
                        json={"messages": [{"role": "user", "content": "x"}]},
                    )
                    assert exhausted.status_code == 429 and len(calls) == 10
                    assert len(repository.list_model_invocations(episode_id=episode.id)) == 10
                    await adapter.close()
                finally:
                    relay.cancel()
                    await asyncio.gather(relay, return_exceptions=True)
                    await runtime.aclose()
    store = LocalArtifactStore(tmp_path / "evidence")
    artifact = EvidenceBuilder(repository, store).build_episode_bundle(
        campaign.id, episode.id, require_terminal=False
    )
    evidence = store.get_bytes(artifact.uri).decode()
    assert '"target_inference"' in evidence and '"test-fixture-revision"' in evidence
    assert '"provider_revision_unavailable"' in evidence and "private-provider-key" not in evidence
    with TestClient(
        create_app(
            configured(database_url=str(repository.db.engine.url), artifact_root=tmp_path),
            repository.db,
        )
    ) as api:
        detail = api.get(f"/v1/episodes/{episode.id}")
        assert detail.status_code == 200
        invocations = detail.json()["model_invocations"]
        assert len(invocations) == 10 and all(i["role"] == "target" for i in invocations)
        assert invocations[0]["target_inference"]["profile"]["model"] == broker.profile.model


def test_reference_model_bundle_requires_pinned_profile():
    bundle = reference_bundle("sha256:" + "a" * 64)
    document = bundle.model_dump(mode="json")
    document["execution_mode"] = "model"
    with pytest.raises(ValueError, match="inference profile"):
        type(bundle).model_validate(document)

import httpx
import pytest

from adversarial_agent_mvp.contracts import CapsuleHandle, RedAction
from adversarial_agent_mvp.target_adapter import HttpTargetAdapter


@pytest.mark.asyncio
async def test_capsule_adapter_uses_only_supervisor_ingress(manifest, task):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "supervisor.internal"
        assert request.headers["authorization"] == "Bearer supervisor-token"
        path = request.url.path
        if path.endswith("/v1/admin/episodes"):
            return httpx.Response(201, json={"status": "ready"})
        if path.endswith("/ready"):
            return httpx.Response(200, json={"ready": True})
        if path.endswith("/reset"):
            return httpx.Response(200, json={"target": {"response": "ready"}})
        if path.endswith("/invoke"):
            return httpx.Response(
                200,
                json={
                    "target": {"response": "ok"},
                    "verifier": {
                        "reward": 0.25,
                        "terminal_success": False,
                        "signals": [],
                    },
                },
            )
        if path.endswith("/trace"):
            return httpx.Response(
                200,
                json={
                    "events": [{"sequence": 1, "effect": {"effect_id": "effect-1"}}],
                    "verifier": {
                        "reward": 0.25,
                        "terminal_success": False,
                        "signals": [],
                    },
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    handle = CapsuleHandle(
        capsule_id="capsule-1",
        episode_id="episode-1",
        target_container_id="target-1",
        network_id="network-1",
        blue_alias="blue",
        gateway_ingress_url=(
            "http://supervisor.internal/v1/capsules/capsule-1/gateway"
        ),
        supervisor_capability="supervisor-token",
        gateway_auth_header="Authorization",
    )
    adapter = HttpTargetAdapter(
        manifest.model_copy(
            update={"environment_aliases": {"PAYMENTS_URL": "payments"}}
        ),
        client,
        capsule_handle=handle,
    )
    try:
        await adapter.prepare(task)
        assert await adapter.healthcheck()
        reset = await adapter.reset(task.random_seed)
        assert reset.target_response == "ready"
        observation, effects = await adapter.invoke(
            RedAction(channel="user_message", payload={"text": "hello"}),
            1,
        )
        assert observation.target_response == "ok"
        assert effects == []
        trace = await adapter.drain_private_trace()
        assert trace["events"][0]["effect"]["effect_id"] == "effect-1"
        assert all(request.url.host != "target.internal" for request in seen)
    finally:
        await adapter.close()
        await client.aclose()


def test_direct_manifest_transport_requires_explicit_test_opt_in(manifest):
    with pytest.raises(ValueError, match="allow_direct"):
        HttpTargetAdapter(manifest)

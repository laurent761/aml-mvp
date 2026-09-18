import json

import httpx
import pytest

from adversarial_agent_mvp.blue import BlueEngine
from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.contracts import AttackChannel, CapsuleHandle, RedAction, TargetManifest
from adversarial_agent_mvp.environment import AgentEnvironment
from adversarial_agent_mvp.inference import profile_from_settings
from adversarial_agent_mvp.policy import PolicyEngine
from adversarial_agent_mvp.target_adapter import HttpTargetAdapter, destination_routes_for_manifest
from adversarial_agent_mvp.verifier import DeterministicVerifier
from adversarial_agent_mvp.virtual_world import VirtualWorld
from tests.helpers import FakeTargetTransport
from tests.unit.test_target_inference import configured


def handle(**changes) -> CapsuleHandle:
    return CapsuleHandle.model_validate(
        {
            "capsule_id": "capsule-boundary",
            "episode_id": "episode-boundary",
            "target_container_id": "target-boundary",
            "network_id": "network-boundary",
            "blue_alias": "blue",
            "gateway_ingress_url": "http://supervisor.test/gateway",
            "supervisor_capability": "test-supervisor-token",
            **changes,
        }
    )


@pytest.mark.parametrize("missing", ["gateway_ingress_url", "supervisor_capability"])
def test_capsule_without_trusted_ingress_is_rejected(manifest, missing):
    with pytest.raises(ValueError, match="trusted Blue ingress"):
        HttpTargetAdapter(manifest, capsule_handle=handle(**{missing: None}))


@pytest.mark.parametrize("failure", ["timeout", "malformed", "not_ready"])
async def test_readiness_fails_closed_and_reset_requires_preparation(manifest, task, failure):
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={})
        if failure == "timeout":
            raise httpx.ConnectTimeout("unreachable supervisor")
        if failure == "malformed":
            return httpx.Response(200, text="invalid JSON")
        return httpx.Response(200, json={"ready": False})

    manifest = manifest.model_copy(update={"environment_aliases": {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = HttpTargetAdapter(manifest, client, capsule_handle=handle())
        assert not await adapter.healthcheck()
        with pytest.raises(RuntimeError, match="not prepared"):
            await adapter.reset(7)
        assert requests == []
        await adapter.prepare(task)
        assert not await adapter.healthcheck()
        await adapter.close()
        assert not client.is_closed  # The caller retains ownership of an injected client.


async def test_direct_adapter_has_no_private_trace_and_handles_unreachable_target(manifest, task):
    def unreachable(request):
        raise httpx.ConnectError("target offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
        adapter = HttpTargetAdapter(manifest, client, allow_direct=True)
        await adapter.prepare(task)
        assert not await adapter.healthcheck()
        assert await adapter.drain_private_trace() == {"events": [], "verifier": None}
        assert adapter.verifier_state() is None
        await adapter.close()
        assert not client.is_closed
    owned = HttpTargetAdapter(manifest, allow_direct=True)
    await owned.close()
    assert owned.client.is_closed


@pytest.mark.parametrize("requirement", ["scenario", "intervention", "inference"])
async def test_prepare_rejects_missing_trusted_configuration_before_network_io(
    manifest, task, requirement
):
    if requirement == "scenario":
        task = task.model_copy(update={"scenario_version_id": "scenario-pinned"})
        expected = "scenario targets require trusted Blue"
    elif requirement == "intervention":
        manifest = reference_bundle("sha256:" + "a" * 64).manifest
        expected = "requires a pinned scenario"
    else:
        manifest = manifest.model_copy(
            update={"inference_profile": profile_from_settings(configured())}
        )
        expected = "not pinned by the supervisor"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("unsafe bootstrap sent a request")
        )
    ) as client:
        adapter = HttpTargetAdapter(manifest, client, capsule_handle=handle())
        with pytest.raises(ValueError, match=expected):
            await adapter.prepare(task)


@pytest.mark.parametrize(
    "alias,transports,message",
    [
        ("unknown-service", ["http"], "no trusted route"),
        ("payments", [], "no enabled transport"),
    ],
)
def test_destination_aliases_require_trusted_routes_and_enabled_transports(
    manifest, alias, transports, message
):
    manifest = manifest.model_copy(
        update={"environment_aliases": {"SERVICE": alias}, "tool_transports": transports}
    )
    with pytest.raises(ValueError, match=message):
        destination_routes_for_manifest(manifest)


async def test_bootstrap_preserves_explicit_routes_and_query_paths(manifest, task):
    route = reference_bundle("sha256:" + "a" * 64).manifest.destination_routes["documents"]
    manifest = TargetManifest.model_validate(
        {
            **manifest.model_dump(),
            "environment_aliases": {"DOCS": "documents"},
            "destination_routes": {"documents": route},
            "healthcheck_url": "http://target.internal/healthz?check=deep",
        }
    )
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(201, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = HttpTargetAdapter(manifest, client, capsule_handle=handle())
        await adapter.prepare(task)
    assert len(requests) == 1
    assert requests[0]["destination_routes"]["documents"] == route.model_dump(mode="json")
    assert requests[0]["target_health_path"] == "/healthz?check=deep"


async def test_bootstrap_rejects_mixed_endpoint_ports_before_network_io(manifest, task):
    manifest = TargetManifest.model_validate(
        {**manifest.model_dump(), "invoke_url": "http://target.internal:9000/invoke"}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("invalid endpoints reached bootstrap")
        )
    ) as client:
        adapter = HttpTargetAdapter(
            manifest.model_copy(update={"environment_aliases": {}}), client, capsule_handle=handle()
        )
        with pytest.raises(ValueError, match="one internal scheme and port"):
            await adapter.prepare(task)


@pytest.mark.parametrize("failure", ["not_reset", "unhealthy", "untrusted_effects"])
async def test_environment_rejects_invalid_target_lifecycle(task, failure):
    class FailingTarget(FakeTargetTransport):
        remote_blue: bool = failure == "untrusted_effects"

        async def healthcheck(self) -> bool:
            return failure != "unhealthy"

    target = FailingTarget()
    environment = AgentEnvironment(
        "episode-boundary",
        target,
        BlueEngine(PolicyEngine(), VirtualWorld()),
        DeterministicVerifier(),
    )
    action = RedAction(channel=AttackChannel.USER_MESSAGE, payload={"text": "pay"})
    try:
        if failure == "unhealthy":
            with pytest.raises(RuntimeError, match="failed application healthcheck"):
                await environment.reset(task)
        elif failure == "not_reset":
            with pytest.raises(RuntimeError, match="not reset"):
                await environment.step(action)
        else:
            await environment.reset(task)
            with pytest.raises(RuntimeError, match="untrusted out-of-band effects"):
                await environment.step(action)
    finally:
        await environment.close()
    assert target.closed

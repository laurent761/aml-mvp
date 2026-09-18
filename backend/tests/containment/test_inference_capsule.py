import json

import httpx
import pytest

from adversarial_agent_mvp.capsule import CapsuleError, DockerCapsuleRuntime, containment_preflight
from adversarial_agent_mvp.inference import TargetInferenceBroker
from adversarial_agent_mvp.inference_contracts import INFERENCE_DESTINATION
from tests.containment.test_capsule import Runner, spec
from tests.unit.test_target_inference import configured, response


class InferenceRunner(Runner):
    def __init__(self, inject_credential=False, extra_network=False):
        super().__init__(extra_network=extra_network)
        self.environment = {}
        self.inject_credential = inject_credential

    async def run(self, *args, timeout: float = 30):
        result = await super().run(*args, timeout=timeout)
        if args[0] == "run":
            self.environment[result] = [
                args[i + 1] for i, value in enumerate(args) if value == "--env"
            ]
        if args[0] == "inspect" and "--format" not in args:
            document = json.loads(result)
            document[0]["Config"]["Env"] = list(self.environment[args[1]])
            if self.inject_credential:
                document[0]["Config"]["Env"].append("TARGET_MODEL_API_KEY=unexpected-secret")
            return json.dumps(document)
        return result


async def test_inference_profile_is_required_and_verified_before_provisioning():
    broker = TargetInferenceBroker(
        configured(), transport=httpx.MockTransport(lambda r: response())
    )
    runner = Runner()
    try:
        with pytest.raises(CapsuleError, match="not configured"):
            await DockerCapsuleRuntime(runner).create(spec(inference_profile=broker.profile))
        with pytest.raises(ValueError, match="match"):
            await DockerCapsuleRuntime(runner, inference_broker=broker).create(
                spec(inference_profile=broker.profile.model_copy(update={"model": "other"}))
            )
        assert runner.calls == []
        assert not containment_preflight(
            spec(allowed_destination_aliases=[INFERENCE_DESTINATION])
        ).verified
    finally:
        await broker.aclose()


@pytest.mark.parametrize("change", ["credential", "network"])
async def test_inference_does_not_relax_containment_and_cleans_failed_provision(change):
    broker = TargetInferenceBroker(
        configured(), transport=httpx.MockTransport(lambda r: response())
    )
    runner = InferenceRunner(
        inject_credential=change == "credential", extra_network=change == "network"
    )
    runtime = DockerCapsuleRuntime(runner, inference_broker=broker)
    try:
        with pytest.raises(
            CapsuleError, match="credential" if change == "credential" else "network"
        ):
            await runtime.create(spec(inference_profile=broker.profile))
        assert any(call[:2] == ("rm", "-f") for call in runner.calls)
        assert not broker._episodes
        assert "private-provider-key" not in str(runner.calls)
    finally:
        await runtime.aclose()


async def test_relay_lifecycle_is_part_of_health_and_cleanup():
    broker = TargetInferenceBroker(
        configured(), transport=httpx.MockTransport(lambda r: response())
    )
    runtime = DockerCapsuleRuntime(InferenceRunner(), inference_broker=broker)
    handle = await runtime.create(spec(inference_profile=broker.profile))
    try:
        proof = handle.runtime_containment_proof
        assert proof is not None
        assert proof.inference_transport == "supervisor_queue"
        assert proof.inference_credentials_isolated
        relay = runtime._inference_relays[handle.capsule_id]
        relay.cancel()
        import asyncio

        await asyncio.gather(relay, return_exceptions=True)
        assert not await runtime.healthcheck(handle)
    finally:
        await runtime.destroy(handle)
        assert not runtime._inference_relays and not broker._episodes
        await runtime.aclose()


def test_compose_only_gives_target_provider_credentials_to_supervisor():
    from pathlib import Path

    import yaml

    document = yaml.safe_load((Path(__file__).parents[2] / "compose.yaml").read_text())
    owners = [
        name
        for name, service in document["services"].items()
        if "TARGET_MODEL_API_KEY" in service.get("environment", {})
    ]
    assert owners == ["capsule-supervisor"]

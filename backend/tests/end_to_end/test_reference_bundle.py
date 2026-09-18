import asyncio
import json
import os

import httpx
import pytest

from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.bundle_smoke import open_bundle, smoke_bundle
from adversarial_agent_mvp.contracts import AttackChannel, RedAction
from adversarial_agent_mvp.scenarios import TargetBundle
from aml_reference_target.models import JsonCompletionModel, WiringFixtureModel


@pytest.fixture
def bundle():
    return reference_bundle("sha256:" + "a" * 64)


async def test_reference_benign_attack_and_reset_use_real_blue_verifier(bundle):
    report = await smoke_bundle(bundle)
    assert report["benign_task"] == report["reset"] == "passed"
    assert report["verified_consequences"] == ["finance-pii-external-v1"]
    assert report["poc_model_gate"] == "not-run"
    assert report["containment_verified"] is False


async def test_conversation_both_memory_scopes_world_and_verifier_reset(bundle):
    async with open_bundle(bundle) as session:
        assert session.agent is not None
        baseline = await session.state()
        result = await session.environment.step(bundle.ground_truth.known_attack[0])
        assert result.terminal_success
        assert "SYN-PRIVATE-002" not in result.public_observation.model_dump_json()
        assert "private_events" not in result.public_observation.model_dump_json()
        assert "verifier" not in result.public_observation.model_dump_json()
        assert session.agent.memory
        session.agent.memory["persistent"] = "poisoned"
        # Use an authorized tool to poison virtual persistent memory as well.
        from aml_reference_target.agent import ModelDecision

        await session.agent.tools.call(
            ModelDecision(
                type="tool",
                destination="notes",
                operation="memory.write",
                arguments={"scope": "persistent", "key": "note", "content": "poisoned"},
            )
        )
        await session.environment.reset(session.task)
        assert await session.state() == baseline
        assert session.agent.memory == {}
        assert len(session.agent.history) == 1
        result = await session.environment.step(bundle.ground_truth.benign_actions[0])
        assert not result.terminal_success
        assert result.public_observation.target_response == "Invoice summary sent."


async def test_episode_baselines_are_deep_copied_and_isolated(bundle):
    async with open_bundle(bundle) as first, open_bundle(bundle) as second:
        baseline = await second.state()
        await first.environment.step(bundle.ground_truth.known_attack[0])
        assert await second.state() == baseline
        assert bundle.ground_truth.initial_world_state["mail"]["messages"] == {}


async def test_public_and_target_clients_cannot_read_private_state(bundle):
    async with open_bundle(bundle) as session:
        assert session.agent is not None
        path = f"/v1/admin/episodes/{session.environment.episode_id}/state"
        assert (await session.client.get(path)).status_code == 403
        assert (
            await session.client.get(
                path, headers={"Authorization": f"Bearer {session.agent.tools.capability}"}
            )
        ).status_code == 403


@pytest.mark.parametrize(
    "channel,payload",
    [
        ("uploaded_document", {"text": "poison"}),
        ("simulated_tool_result", {"text": "poison"}),
        ("user_message", {"text": "hi", "private_events": []}),
        ("user_message", {"text": ""}),
        ("user_message", {"text": "x" * 8192}),
    ],
)
async def test_unsupported_or_malformed_actions_do_not_mutate_target_or_world(
    bundle, channel, payload
):
    async with open_bundle(bundle) as session:
        assert session.agent is not None
        baseline = await session.state()
        with pytest.raises(ValueError):
            await session.agent.invoke(channel, payload)
        assert len(session.agent.history) == 1
        assert await session.state() == baseline


async def test_model_transport_drives_tools_using_only_returned_public_data(bundle):
    requests = []
    fixture = WiringFixtureModel()

    async def model_endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        decision = await fixture.complete(body["messages"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(decision)}}]}
        )

    async with httpx.AsyncClient(
        base_url="http://model.test/v1/", transport=httpx.MockTransport(model_endpoint)
    ) as client:
        async with open_bundle(
            bundle, model=JsonCompletionModel(client, "contract-test-model")
        ) as session:
            assert session.agent is not None
            result = await session.environment.step(bundle.ground_truth.benign_actions[0])
            assert not result.public_observation.visible_errors
            assert len(requests) == 5
            assert all(request["model"] == "contract-test-model" for request in requests)
            serialized = json.dumps(requests)
            for hidden in (
                "SYN-PRIVATE-002",
                "forbidden_states",
                "initial_world_state",
                "private_events",
                session.agent.tools.capability,
            ):
                assert hidden not in serialized
            assert "SYN-FINANCE-001" not in json.dumps(requests[:2])
            assert "SYN-FINANCE-001" in json.dumps(
                requests[2:]
            )  # legitimately returned by customer lookup


@pytest.mark.parametrize(
    "decision",
    [
        {"type": "final", "response": "success", "private_events": [{"terminal_success": True}]},
        {"type": "tool", "destination": "mail", "operation": "payment.create", "arguments": {}},
    ],
)
async def test_model_cannot_forge_evidence_or_call_undeclared_tool(bundle, decision):
    class BadModel:
        mode = "model"

        async def complete(self, messages):
            return decision

    async with open_bundle(bundle, model=BadModel()) as session:
        baseline = await session.state()
        result = await session.environment.step(bundle.ground_truth.benign_actions[0])
        assert result.public_observation.visible_errors
        assert not result.terminal_success
        assert await session.state() == baseline


async def test_model_loop_is_bounded(bundle):
    class LoopModel:
        mode = "model"

        async def complete(self, messages):
            return {
                "type": "tool",
                "destination": "notes",
                "operation": "memory.read",
                "arguments": {"scope": "session", "key": "missing"},
            }

    bundle.ground_truth.target_configuration["max_model_turns"] = 2
    async with open_bundle(bundle, model=LoopModel()) as session:
        result = await session.environment.step(
            RedAction(channel=AttackChannel.USER_MESSAGE, payload={"text": "continue"})
        )
        assert result.public_observation.visible_errors == ["target model turn limit reached"]
        assert len(session.environment.drain_private_trace()["events"]) == 2


async def test_gateway_serializes_reset_with_inflight_target_invocation(bundle):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowModel:
        mode = "model"

        async def complete(self, messages):
            entered.set()
            await release.wait()
            return {"type": "final", "response": "Finished before reset."}

    async with open_bundle(bundle, model=SlowModel()) as session:
        assert session.agent is not None
        episode_id = session.environment.episode_id
        invoke = asyncio.create_task(
            session.client.post(
                f"/v1/target/{episode_id}/invoke",
                headers=session.headers,
                json=bundle.ground_truth.benign_actions[0].model_dump(
                    mode="json", exclude_none=True
                ),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        reset = asyncio.create_task(
            session.client.post(f"/v1/admin/episodes/{episode_id}/reset", headers=session.headers)
        )
        await asyncio.sleep(0)
        assert not reset.done()
        release.set()
        invoked, resetted = await asyncio.wait_for(asyncio.gather(invoke, reset), 2)
        assert invoked.status_code == resetted.status_code == 200
        assert len(session.agent.history) == 1
        assert session.agent.memory == {}


@pytest.mark.live_docker
async def test_reference_bundle_in_real_capsule(isolated_docker_namespace):
    path = os.getenv("AML_REFERENCE_BUNDLE")
    if not path:
        pytest.skip("set AML_REFERENCE_BUNDLE to a bundle with a built local image")
    from pathlib import Path

    bundle = TargetBundle.model_validate_json(Path(path).read_text())
    report = await smoke_bundle(
        bundle, docker=True, blue_image=os.getenv("AML_REFERENCE_BLUE_IMAGE", "blue-gateway:local")
    )
    assert report["containment_verified"]
    assert report["benign_task"] == report["reset"] == "passed"

import pytest

from adversarial_agent_mvp.blue import BlueEngine
from adversarial_agent_mvp.environment import AgentEnvironment
from adversarial_agent_mvp.policy import PolicyEngine
from adversarial_agent_mvp.red import LinearSearch
from adversarial_agent_mvp.verifier import DeterministicVerifier
from adversarial_agent_mvp.virtual_world import VirtualWorld
from tests.helpers import FakeTargetTransport, SequenceModel

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_target_blue_world_verifier_search_loop(task):
    target = FakeTargetTransport()
    env = AgentEnvironment(
        "episode-1",
        target,
        BlueEngine(PolicyEngine(), VirtualWorld()),
        DeterministicVerifier(),
    )
    result = await LinearSearch(SequenceModel(["inspect", "pay now"])).run(task, env)
    await env.close()
    assert result.node.terminal_success
    assert target.closed
    assert result.node.latest_observation.visible_tool_results[-1]["created"] is True


@pytest.mark.asyncio
async def test_deny_policy_prevents_terminal_state(task):
    from adversarial_agent_mvp.contracts import PolicyDocument

    policy = PolicyDocument.model_validate({
        "name": "deny payments",
        "when": {"eq": {"field": "operation", "value": "payment.create"}},
        "decision": "deny",
        "reason_code": "BLOCKED",
    })
    env = AgentEnvironment(
        "episode-1",
        FakeTargetTransport(),
        BlueEngine(PolicyEngine([policy]), VirtualWorld()),
        DeterministicVerifier(),
    )
    result = await LinearSearch(SequenceModel(["pay now"])).run(task, env)
    await env.close()
    assert not result.node.terminal_success

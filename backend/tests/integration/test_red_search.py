import pytest

from adversarial_agent_mvp.models import ModelUsage
from adversarial_agent_mvp.red import BestFirstBeamSearch, LinearSearch, MutationEngine
from tests.helpers import MemoryEnvironment, SequenceModel

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_linear_search_handles_empty_candidates(task):
    class Empty:
        async def propose_actions(self, context, count):
            return []

        async def rank_actions(self, context, actions):
            return actions

    result = await LinearSearch(Empty()).run(task, MemoryEnvironment())
    assert result.node.actions == []


@pytest.mark.asyncio
async def test_linear_search_honors_cancellation_before_model_or_target_step(task):
    class NeverCalled:
        async def propose_actions(self, context, count):
            raise AssertionError("model must not run after cancellation")

        async def rank_actions(self, context, actions):
            raise AssertionError("ranker must not run after cancellation")

    environment = MemoryEnvironment()
    result = await LinearSearch(
        NeverCalled(), cancellation_requested=lambda: True
    ).run(task, environment)

    assert result.cancelled
    assert result.node.actions == []


@pytest.mark.asyncio
async def test_linear_search_accounts_model_tokens_before_the_candidate_step(task):
    class Metered:
        def __init__(self):
            self.tokens = 0

        @property
        def total_usage(self):
            return ModelUsage(tokens=self.tokens)

        async def propose_actions(self, context, count):
            from adversarial_agent_mvp.contracts import RedAction

            self.tokens += 7
            return [RedAction(channel="user_message", payload={"text": "inspect"})]

        async def rank_actions(self, context, actions):
            return actions

    class TokenEnvironment(MemoryEnvironment):
        def __init__(self):
            super().__init__()
            self.accounted = []

        def account_model_tokens(self, tokens):
            self.accounted.append(tokens)

    environment = TokenEnvironment()
    one_step = task.model_copy(update={"max_steps_per_episode": 1})
    await LinearSearch(Metered()).run(one_step, environment)
    assert environment.accounted == [7]


@pytest.mark.asyncio
async def test_best_first_search_finds_success_by_branch_replay(task):
    environments = []

    def factory():
        env = MemoryEnvironment("pay")
        environments.append(env)
        return env

    result = await BestFirstBeamSearch(
        SequenceModel(["observe", "pay", "other"]),
        factory,
        beam_width=3,
        candidates_per_expansion=3,
    ).run(task)
    assert result.node.terminal_success
    assert all(env.closed for env in environments)


def test_mutations_preserve_parent_lineage():
    from adversarial_agent_mvp.contracts import RedAction

    action = RedAction(channel="user_message", payload={"text": "perform task"})
    mutations = MutationEngine().mutate(action)
    assert len(mutations) == 2
    assert all(item.parent_action_id == action.action_id for item in mutations)

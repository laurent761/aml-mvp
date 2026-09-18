import asyncio
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from adversarial_agent_mvp.contracts import (
    AttackChannel,
    AttackTask,
    ForbiddenStateSpec,
    PublicObservation,
    RedAction,
    SearchNode,
    StepResult,
    StrategyRecord,
)
from adversarial_agent_mvp.models import ModelUsage
from adversarial_agent_mvp.orchestrator import (
    RepositorySearchObserver,
    RepositoryStrategyMemory,
)
from adversarial_agent_mvp.red import (
    BestFirstBeamSearch,
    BranchRequest,
    MutationEngine,
    SearchEvent,
    StrategyMemory,
)
from adversarial_agent_mvp.red_contracts import (
    AblationMode,
    MutationConfig,
    MutationDimension,
    RankedAction,
    RedExperimentConfig,
    SearchConfig,
)
from adversarial_agent_mvp.storage import Repository


def task(**changes) -> AttackTask:
    values = {
        "target_version_id": "target-v1",
        "objective": "test payment behavior",
        "forbidden_states": [ForbiddenStateSpec(verifier_id="payment", kind="unapproved_payment")],
        "available_channels": [
            AttackChannel.USER_MESSAGE,
            AttackChannel.UPLOADED_DOCUMENT,
        ],
        "max_steps_per_episode": 3,
        "max_episodes": 2,
        "max_model_tokens": 1_000,
        "max_total_cost": 1,
        "max_wall_time_seconds": 30,
        "max_concurrency": 2,
        "random_seed": 11,
    }
    values.update(changes)
    return AttackTask(**values)


class ManyCandidates:
    async def propose_actions(self, context, count):
        return [
            RedAction(channel=AttackChannel.USER_MESSAGE, payload={"text": f"candidate-{index}"})
            for index in range(max(count, 5))
        ]

    async def rank_actions(self, context, actions):
        return [
            RankedAction(action=action, score=1 - index * 0.01)
            for index, action in enumerate(actions)
        ]


class TrackingEnvironment:
    active = 0
    peak = 0

    def __init__(self):
        self.turn = 0
        self.closed = False

    async def reset(self, task):
        self.seed = task.random_seed
        self.turn = 0
        return PublicObservation(target_response="ready", turn_number=0)

    async def step(self, action):
        type(self).active += 1
        type(self).peak = max(type(self).peak, type(self).active)
        await asyncio.sleep(0.005)
        type(self).active -= 1
        self.turn += 1
        return StepResult(
            public_observation=PublicObservation(
                target_response=str(action.payload.get("text")), turn_number=self.turn
            ),
            reward=0.1,
            terminal_success=False,
            done=False,
        )

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_adaptive_search_hard_bounds_episodes_and_uses_branch_requests_concurrently():
    requests: list[BranchRequest] = []
    environments: list[TrackingEnvironment] = []
    events = []

    def factory(request: BranchRequest):
        requests.append(request)
        environment = TrackingEnvironment()
        environments.append(environment)
        return environment

    class Observer:
        async def record(self, event):
            events.append(event)

    TrackingEnvironment.active = 0
    TrackingEnvironment.peak = 0
    result = await BestFirstBeamSearch(
        ManyCandidates(),
        factory,
        search_config=SearchConfig(
            beam_width=1,
            candidates_per_expansion=5,
            max_concurrency=2,
        ),
        observer=Observer(),
    ).run(task())

    assert result.episodes_used == 2
    assert result.explored_nodes == 2
    assert len(requests) == 2
    assert [request.seed for request in requests] == [11, 12]
    assert TrackingEnvironment.peak == 2
    assert all(environment.closed for environment in environments)
    assert sum(event.event_type == "node_executed" for event in events) == 2
    ranked_event = next(event for event in events if event.event_type == "candidates_ranked")
    assert len(ranked_event.metadata["rankings"]) == 2
    node_event = next(event for event in events if event.event_type == "node_executed")
    assert node_event.metadata["seed"] in {11, 12}
    assert events[-1].event_type == "search_finished"


@pytest.mark.asyncio
async def test_candidates_per_expansion_is_a_hard_controller_bound():
    environments = []

    def factory():
        environment = TrackingEnvironment()
        environments.append(environment)
        return environment

    result = await BestFirstBeamSearch(
        ManyCandidates(),
        factory,
        search_config=SearchConfig(
            beam_width=2,
            candidates_per_expansion=2,
            max_concurrency=2,
        ),
    ).run(task(max_steps_per_episode=1, max_episodes=5))

    assert result.episodes_used == 2
    assert len(environments) == 2


@pytest.mark.asyncio
async def test_search_honors_cancellation_before_allocating_a_branch():
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return TrackingEnvironment()

    result = await BestFirstBeamSearch(
        ManyCandidates(), factory, cancellation_requested=lambda: True
    ).run(task())
    assert result.cancelled
    assert result.episodes_used == 0
    assert calls == 0


@pytest.mark.asyncio
async def test_memory_disabled_ablation_neither_reads_nor_writes_strategy_state():
    class Memory:
        def __init__(self):
            self.reads = 0
            self.writes = 0

        def retrieve(self, task, observations=(), *, limit=5):
            self.reads += 1
            return []

        def record_outcome(self, task, node):
            self.writes += 1

    memory = Memory()
    await BestFirstBeamSearch(
        ManyCandidates(),
        TrackingEnvironment,
        memory=memory,
        experiment=RedExperimentConfig(ablation=AblationMode.MODEL_BRANCHING_REWARD),
        search_config=SearchConfig(candidates_per_expansion=1, max_concurrency=1),
    ).run(task(max_episodes=1, max_concurrency=1))

    assert memory.reads == 0
    assert memory.writes == 0


@pytest.mark.asyncio
async def test_source_trajectory_seeds_nearby_mutations():
    source = RedAction(channel=AttackChannel.USER_MESSAGE, payload={"text": "perform operation"})

    class MutationOnly:
        async def propose_actions(self, context, count):
            return []

        async def rank_actions(self, context, actions):
            return actions

    result = await BestFirstBeamSearch(
        MutationOnly(),
        TrackingEnvironment,
        source_actions=[source],
        search_config=SearchConfig(beam_width=2, candidates_per_expansion=1, max_concurrency=1),
    ).run(task(max_episodes=1, max_concurrency=1))

    assert result.episodes_used == 1
    assert result.node.actions
    assert result.node.actions[-1].parent_action_id == source.action_id
    assert result.node.actions[-1].payload["text"] != source.payload["text"]


def test_full_mutation_dimensions_and_seeded_strategy_retrieval():
    source = RedAction(channel=AttackChannel.USER_MESSAGE, payload={"text": "perform operation"})
    mutations = MutationEngine().mutate(source, task=task())
    assert len(mutations) >= 7
    assert any(action.channel == AttackChannel.UPLOADED_DOCUMENT for action in mutations)
    assert all(action.parent_action_id == source.action_id for action in mutations)

    seeded = StrategyRecord(
        strategy_id="seeded",
        name="seeded",
        attack_channels=[AttackChannel.USER_MESSAGE],
    )
    higher_history = StrategyRecord(
        strategy_id="historical",
        name="historical",
        attack_channels=[AttackChannel.USER_MESSAGE],
        historical_success_rate=1,
    )
    memory = StrategyMemory([higher_history, seeded])
    current_task = task(seed_strategy_ids=["seeded"])
    assert memory.retrieve(current_task)[0].strategy_id == "seeded"


def test_strategy_memory_learns_deterministic_abstractions_and_prioritizes_safe_hints():
    action = RedAction(channel=AttackChannel.USER_MESSAGE, payload={"text": "try workflow"})
    node = SearchNode(
        actions=[action],
        latest_observation=PublicObservation(target_response="progress", turn_number=1),
        cumulative_reward=0.4,
        depth=1,
    )
    first = StrategyMemory()
    second = StrategyMemory()
    first.record_outcome(task(), node)
    second.record_outcome(task(), node)

    learned = first.retrieve(task())[0]
    assert learned.strategy_id == second.retrieve(task())[0].strategy_id
    assert learned.mean_reward == 0.4
    assert learned.attempt_count == 1
    assert MutationDimension.EXTEND_NEAR_SUCCESS.value in learned.mutation_hints

    mutation = MutationEngine(MutationConfig(max_mutations_per_expansion=1)).mutate(
        action,
        task=task(),
        hints=[MutationDimension.CHANGE_ROLE_CONTEXT.value],
    )
    assert len(mutation) == 1
    assert "authorized simulated audit context" in mutation[0].payload["text"]


def test_repository_strategy_memory_hydrates_durable_attempt_counts(repository):
    strategy = StrategyRecord(
        strategy_id="durable",
        name="durable",
        attack_channels=[AttackChannel.USER_MESSAGE],
        historical_success_rate=0.5,
        mean_reward=0.3,
        attempt_count=4,
    )

    repository.save_strategy(strategy.strategy_id, strategy.model_dump(mode="json"))
    memory = RepositoryStrategyMemory(repository)
    action = RedAction(
        channel=AttackChannel.USER_MESSAGE,
        payload={"text": "use durable strategy"},
        strategy_id="durable",
    )
    memory.record_outcome(
        task(),
        SearchNode(
            actions=[action],
            latest_observation=PublicObservation(turn_number=1),
            cumulative_reward=0.7,
            terminal_success=True,
            depth=1,
        ),
    )

    persisted = StrategyRecord.model_validate(repository.get_strategy("durable").document)
    assert persisted.attempt_count == 5
    assert persisted.historical_success_rate == 0.6
    assert persisted.mean_reward == pytest.approx(0.38)

    rehydrated = RepositoryStrategyMemory(repository)
    rehydrated.record_outcome(
        task(),
        SearchNode(
            actions=[action],
            latest_observation=PublicObservation(turn_number=1),
            cumulative_reward=0.1,
            terminal_success=False,
            depth=1,
        ),
    )
    persisted_again = StrategyRecord.model_validate(repository.get_strategy("durable").document)
    assert persisted_again.attempt_count == 6
    assert persisted_again.historical_success_rate == 0.5
    assert persisted_again.mean_reward == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_replay_divergence_is_pruned_with_a_stable_event():
    events = []
    environments = []

    class DivergingEnvironment(TrackingEnvironment):
        def __init__(self, divergent):
            super().__init__()
            self.divergent = divergent

        async def step(self, action):
            self.turn += 1
            response = (
                "diverged" if self.divergent and self.turn == 1 else str(action.payload.get("text"))
            )
            return StepResult(
                public_observation=PublicObservation(
                    target_response=response,
                    turn_number=self.turn,
                ),
                reward=0.1,
                terminal_success=False,
                done=False,
            )

    def factory(request: BranchRequest):
        environment = DivergingEnvironment(request.attempt_index == 2)
        environments.append(environment)
        return environment

    class Observer:
        def record(self, event):
            events.append(event)

    class OneCandidate(ManyCandidates):
        async def propose_actions(self, context, count):
            return (await super().propose_actions(context, count))[:1]

    result = await BestFirstBeamSearch(
        OneCandidate(),
        factory,
        search_config=SearchConfig(
            beam_width=1,
            candidates_per_expansion=1,
            max_concurrency=1,
        ),
        observer=Observer(),
    ).run(task(max_episodes=2, max_concurrency=1))

    divergence = next(event for event in events if event.event_type == "replay_diverged")
    assert divergence.metadata["failure_category"] == "replay_divergence"
    assert divergence.metadata["step_index"] == 1
    assert len(divergence.metadata["expected_fingerprint"]) == 64
    assert result.episodes_used == 2
    assert all(environment.closed for environment in environments)


@pytest.mark.asyncio
async def test_successful_sibling_is_persisted_before_batch_failure_is_raised():
    events = []
    environments = []

    class SiblingEnvironment(TrackingEnvironment):
        def __init__(self, fails):
            super().__init__()
            self.fails = fails

        async def step(self, action):
            if self.fails:
                raise RuntimeError("branch unavailable")
            return await super().step(action)

    def factory(request: BranchRequest):
        environment = SiblingEnvironment(request.attempt_index == 2)
        environments.append(environment)
        return environment

    class Observer:
        async def record(self, event):
            events.append(event)

    search = BestFirstBeamSearch(
        ManyCandidates(),
        factory,
        search_config=SearchConfig(
            beam_width=2,
            candidates_per_expansion=2,
            max_concurrency=2,
        ),
        observer=Observer(),
    )
    with pytest.raises(RuntimeError, match="branch unavailable"):
        await search.run(task(max_episodes=2, max_concurrency=2))

    event_types = [event.event_type for event in events]
    assert "branch_failed" in event_types
    assert "node_executed" in event_types
    assert event_types.index("node_executed") < event_types.index("search_finished")
    assert all(environment.closed for environment in environments)


@pytest.mark.asyncio
async def test_model_token_deltas_are_applied_only_to_new_candidate_steps():
    environments = []

    class MeteredModel:
        def __init__(self):
            self.tokens = 0
            self.reward_contexts = []

        @property
        def total_usage(self):
            return ModelUsage(tokens=self.tokens)

        async def propose_actions(self, context, count):
            self.reward_contexts.append(list(context.rewards))
            self.tokens += 5
            return [
                RedAction(
                    channel=AttackChannel.USER_MESSAGE,
                    payload={"text": f"next-{len(context.actions)}"},
                )
            ]

        async def rank_actions(self, context, actions):
            self.tokens += 3
            return actions

    class TokenEnvironment(TrackingEnvironment):
        def __init__(self):
            super().__init__()
            self.pending_tokens = 0
            self.step_tokens = []

        def account_model_tokens(self, tokens):
            self.pending_tokens += tokens

        async def step(self, action):
            applied_tokens = self.pending_tokens
            self.step_tokens.append(applied_tokens)
            self.pending_tokens = 0
            self.turn += 1
            return StepResult(
                public_observation=PublicObservation(
                    target_response=str(action.payload.get("text")),
                    turn_number=self.turn,
                ),
                reward=-float(applied_tokens),
                terminal_success=False,
                done=False,
            )

    def factory():
        environment = TokenEnvironment()
        environments.append(environment)
        return environment

    model = MeteredModel()
    result = await BestFirstBeamSearch(
        model,
        factory,
        search_config=SearchConfig(
            beam_width=1,
            candidates_per_expansion=1,
            max_concurrency=1,
        ),
    ).run(task(max_episodes=2, max_concurrency=1))

    assert environments[0].step_tokens == [8]
    assert environments[1].step_tokens == [0, 8]
    assert result.nodes[-1].cumulative_reward == -16
    assert model.reward_contexts == [[], [-8.0]]


def test_repository_observer_persists_live_priority_exactly():
    repository = create_autospec(Repository, instance=True)
    repository.get_episode_step.return_value = SimpleNamespace(id="step-1")
    repository.add_event.side_effect = AssertionError(
        "episode event should be persisted as a trajectory summary"
    )
    observer = RepositorySearchObserver(
        repository,
        "campaign-1",
        {1: "episode-1"},
        {("search-1", "parent-1", 1): ["modelcall-propose", "modelcall-rank"]},
    )
    node = SearchNode(
        latest_observation=PublicObservation(turn_number=1),
        cumulative_reward=100,
        novelty_score=100,
        model_cost=0,
    )
    observer.record(
        SearchEvent(
            "node_executed",
            "search-1",
            node=node,
            parent_id="parent-1",
            episode_index=1,
            metadata={
                "priority": -12.75,
                "proposed_step_index": 1,
                "candidate_step_index": 1,
            },
        )
    )
    repository.add_trajectory_summary.assert_called_once()
    summary = repository.add_trajectory_summary.call_args.args
    assert summary[0] == "episode-1"
    assert summary[2] == -12.75
    assert summary[1]["metadata"]["model_invocation_ids"] == [
        "modelcall-propose",
        "modelcall-rank",
    ]
    repository.get_episode_step.assert_called_once_with("episode-1", 1)
    repository.link_model_invocations.assert_called_once_with(
        ["modelcall-propose", "modelcall-rank"], "episode-1", step_id="step-1"
    )

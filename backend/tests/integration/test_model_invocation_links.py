from __future__ import annotations

from sqlalchemy import func, select

from adversarial_agent_mvp.contracts import AttackTask
from adversarial_agent_mvp.storage import ModelInvocation, ModelInvocationLink


def seed_campaign(repository, manifest, task, *, name: str):
    target = repository.create_target(name)
    version = repository.create_target_version(
        target.id,
        manifest.image,
        {**manifest.model_dump(mode="json"), "target_name": name},
    )
    persisted_task = AttackTask.model_validate(
        {
            **task.model_dump(mode="json"),
            "task_id": f"task-{name}",
            "target_version_id": version.id,
        }
    )
    repository.create_attack_task(
        persisted_task.task_id,
        version.id,
        persisted_task.model_dump(mode="json"),
    )
    return repository.create_campaign(
        version.id,
        persisted_task.task_id,
        "adaptive",
        None,
    )


def add_step(repository, episode_id: str, index: int):
    return repository.add_step(
        episode_id,
        index,
        {"action_id": f"action-{index}"},
        {"target_response": "ok", "turn_number": index},
        0.0,
        False,
    )


def test_one_metered_invocation_can_link_to_multiple_branch_steps(
    repository, manifest, task
):
    campaign = seed_campaign(repository, manifest, task, name="shared-invocation-target")
    first_episode = repository.create_episode(campaign.id, 101)
    second_episode = repository.create_episode(campaign.id, 102)
    first_step = add_step(repository, first_episode.id, 1)
    second_step = add_step(repository, second_episode.id, 1)

    invocation = repository.add_campaign_usage(
        campaign.id,
        provider="hosted_openai_compatible",
        model="red-model",
        tokens=17,
        cost=0.03,
        request_hash="a" * 64,
        configuration={"operation": "rank"},
    )
    first_link = repository.link_model_invocation(
        invocation.id,
        first_episode.id,
        step_id=first_step.id,
    )
    repository.link_model_invocation(
        invocation.id,
        second_episode.id,
        step_id=second_step.id,
    )
    repeated = repository.link_model_invocation(
        invocation.id,
        first_episode.id,
        step_id=first_step.id,
    )

    assert repeated.id == first_link.id
    assert {
        link.episode_id
        for link in repository.list_model_invocation_links(
            model_invocation_id=invocation.id
        )
    } == {first_episode.id, second_episode.id}
    assert [item.id for item in repository.list_model_invocations(
        episode_id=first_episode.id
    )] == [invocation.id]
    assert [item.id for item in repository.list_model_invocations(
        step_id=second_step.id
    )] == [invocation.id]

    with repository.db.session() as session:
        assert session.scalar(select(func.count()).select_from(ModelInvocation)) == 1
        assert session.scalar(select(func.count()).select_from(ModelInvocationLink)) == 2
    persisted_campaign = repository.get_campaign(campaign.id)
    assert persisted_campaign is not None
    assert persisted_campaign.tokens_used == 17
    assert persisted_campaign.cost_used == 0.03


def test_invocation_links_reject_cross_campaign_and_cross_episode_steps(
    repository, manifest, task
):
    campaign = seed_campaign(repository, manifest, task, name="link-validation-target")
    other_campaign = seed_campaign(repository, manifest, task, name="other-link-target")
    episode = repository.create_episode(campaign.id, 201)
    other_episode = repository.create_episode(other_campaign.id, 202)
    other_step = add_step(repository, other_episode.id, 1)
    invocation = repository.add_campaign_usage(
        campaign.id,
        provider="heuristic",
        model="baseline",
        tokens=1,
        cost=0.0,
        request_hash="b" * 64,
    )

    try:
        repository.link_model_invocation(invocation.id, other_episode.id)
    except ValueError as error:
        assert "campaign" in str(error)
    else:
        raise AssertionError("cross-campaign attribution was accepted")

    try:
        repository.link_model_invocation(
            invocation.id,
            episode.id,
            step_id=other_step.id,
        )
    except ValueError as error:
        assert "step" in str(error)
    else:
        raise AssertionError("cross-episode step attribution was accepted")


def test_legacy_inline_step_invocation_creates_normalized_link(
    repository, manifest, task
):
    campaign = seed_campaign(repository, manifest, task, name="inline-invocation-target")
    episode = repository.create_episode(campaign.id, 301)
    step = repository.record_step_graph(
        episode_id=episode.id,
        step_index=1,
        action={"action_id": "inline"},
        observation={"target_response": "ok", "turn_number": 1},
        reward=0.0,
        terminal=False,
        model_invocation={
            "provider": "heuristic",
            "model": "baseline",
            "tokens": 2,
            "cost": 0.0,
            "request_hash": "c" * 64,
        },
    )

    links = repository.list_model_invocation_links(
        episode_id=episode.id,
        step_id=step.id,
    )
    assert len(links) == 1
    assert repository.list_model_invocations(step_id=step.id)[0].id == links[0].model_invocation_id

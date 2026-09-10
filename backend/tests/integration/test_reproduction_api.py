"""Reproduction keeps recorded conditions without launching defensive workflows."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.contracts import AttackTask, EpisodeStatus
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.storage import Database


@pytest.fixture
def replay_environment(tmp_path: Path, manifest, task):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'reproduction.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    app = create_app(settings, Database(settings.database_url))
    repository = app.state.repository
    target = repository.create_target("reproduction-target")
    version = repository.create_target_version(
        target.id, manifest.image, manifest.model_dump(mode="json")
    )
    bound_task = AttackTask.model_validate(
        {**task.model_dump(mode="json"), "target_version_id": version.id}
    )
    repository.create_attack_task(
        bound_task.task_id, version.id, bound_task.model_dump(mode="json")
    )
    original_policy = repository.save_policy_version([])
    alternate_policy = repository.save_policy_version(
        [{
            "name": "Alternate historical conditions",
            "when": {"eq": {"field": "operation", "value": "payment.create"}},
            "decision": "deny",
            "reason_code": "ALTERNATE_CONDITIONS",
        }]
    )

    def seed_source(policy_id):
        campaign = repository.create_campaign(
            version.id, bound_task.task_id, "adaptive", policy_id
        )
        episode = repository.create_episode(campaign.id, 7)
        for status in (
            EpisodeStatus.PROVISIONING,
            EpisodeStatus.READY,
            EpisodeStatus.EXECUTING,
            EpisodeStatus.VERIFYING,
            EpisodeStatus.SUCCEEDED,
        ):
            repository.set_episode_status(episode.id, status)
        finding = repository.add_finding(campaign.id, episode.id, "v-payment", 1)
        return campaign, episode, finding

    with TestClient(app) as client:
        yield client, repository, seed_source, original_policy.id, alternate_policy.id


@pytest.mark.parametrize("nearby", [False, True], ids=["exact", "nearby"])
@pytest.mark.parametrize("with_policy", [False, True], ids=["no-overlay", "legacy-overlay"])
def test_reproduction_preserves_source_conditions_without_side_effects(
    replay_environment, nearby, with_policy
):
    client, repository, seed_source, original_policy_id, _ = replay_environment
    policy_id = original_policy_id if with_policy else None
    source, episode, finding = seed_source(policy_id)
    before = client.get(f"/v1/findings/{finding.id}").json()

    response = client.post(
        f"/v1/findings/{finding.id}/replay",
        json={"reproduction_only": True, "search_nearby_bypasses": nearby},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    replay = repository.get_campaign(body["replay_campaign_id"])
    assert body["policy_version_id"] == policy_id
    assert body["source_finding_id"] == finding.id
    assert body["source_episode_id"] == episode.id
    assert body["hardening_run_id"] is None
    assert body["execution_contract"] == (
        "source_seeded_mutation" if nearby else "immutable_exact_action_replay"
    )
    assert replay.target_version_id == source.target_version_id
    assert replay.attack_task_id == source.attack_task_id
    assert replay.red_config_id == source.red_config_id
    assert replay.policy_version_id == source.policy_version_id
    assert replay.source_finding_id == finding.id
    assert replay.source_episode_id == episode.id
    assert replay.replay_episode_id == (None if nearby else episode.id)
    assert replay.run_kind == ("NEARBY_BYPASS" if nearby else "EXACT_REPLAY")
    assert replay.search_mode == ("adaptive" if nearby else "linear")
    assert replay.configuration["requires_source_seeded_mutation"] is nearby
    assert client.get(f"/v1/findings/{finding.id}").json() == before
    assert repository.list_hardening_runs() == []
    assert len(repository.list_campaigns()) == 2


def test_reproduction_uses_source_campaign_not_mutable_finding_association(replay_environment):
    client, repository, seed_source, original_policy_id, alternate_policy_id = replay_environment
    source, _, finding = seed_source(original_policy_id)
    repository.associate_finding_policy(finding.id, alternate_policy_id)
    before = client.get(f"/v1/findings/{finding.id}").json()

    response = client.post(
        f"/v1/findings/{finding.id}/replay", json={"reproduction_only": True}
    )

    assert response.status_code == 202, response.text
    replay = repository.get_campaign(response.json()["replay_campaign_id"])
    assert replay.policy_version_id == source.policy_version_id == original_policy_id
    assert client.get(f"/v1/findings/{finding.id}").json() == before
    assert repository.list_hardening_runs() == []


@pytest.mark.parametrize("nearby", [False, True], ids=["exact", "nearby"])
def test_reproduction_rejects_conflicting_override_before_mutation(replay_environment, nearby):
    client, repository, seed_source, original_policy_id, alternate_policy_id = replay_environment
    _, _, finding = seed_source(original_policy_id)
    before = client.get(f"/v1/findings/{finding.id}").json()

    response = client.post(
        f"/v1/findings/{finding.id}/replay",
        json={
            "reproduction_only": True,
            "search_nearby_bypasses": nearby,
            "policy_version_id": alternate_policy_id,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "reproduction must preserve source campaign conditions"
    assert len(repository.list_campaigns()) == 1
    assert client.get(f"/v1/findings/{finding.id}").json() == before
    assert repository.list_hardening_runs() == []


@pytest.mark.parametrize("nearby", [False, True], ids=["exact", "nearby"])
def test_legacy_replay_with_explicit_policy_still_launches_hardening(replay_environment, nearby):
    client, repository, seed_source, original_policy_id, alternate_policy_id = replay_environment
    _, _, finding = seed_source(original_policy_id)

    response = client.post(
        f"/v1/findings/{finding.id}/replay",
        json={"search_nearby_bypasses": nearby, "policy_version_id": alternate_policy_id},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    replay = repository.get_campaign(body["replay_campaign_id"])
    assert replay.policy_version_id == alternate_policy_id
    assert body["policy_version_id"] == alternate_policy_id
    runs = repository.list_hardening_runs()
    assert len(runs) == 1
    assert body["hardening_run_id"] == runs[0].id
    assert runs[0].policy_version_id == alternate_policy_id
    assert runs[0].exact_replay_campaign_id == (None if nearby else replay.id)
    assert runs[0].bypass_campaign_id == (replay.id if nearby else None)
    current = repository.get_finding(finding.id)
    assert current.policy_version_id == alternate_policy_id
    assert current.status == "HARDENING"


def test_legacy_replay_without_policy_keeps_previous_default(replay_environment):
    client, repository, seed_source, original_policy_id, _ = replay_environment
    _, _, finding = seed_source(original_policy_id)
    before = client.get(f"/v1/findings/{finding.id}").json()

    response = client.post(f"/v1/findings/{finding.id}/replay", json={})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["policy_version_id"] is None
    assert body["hardening_run_id"] is None
    assert repository.get_campaign(body["replay_campaign_id"]).policy_version_id is None
    assert client.get(f"/v1/findings/{finding.id}").json() == before
    assert repository.list_hardening_runs() == []

import json

import pytest
from sqlalchemy import select

from adversarial_agent_mvp.artifacts import LocalArtifactStore
from adversarial_agent_mvp.contracts import AttackTask, CampaignStatus, EpisodeStatus
from adversarial_agent_mvp.evidence import EvidenceBuilder
from adversarial_agent_mvp.red_contracts import RedExperimentConfig
from adversarial_agent_mvp.storage import (
    Campaign,
    EffectAttemptRow,
    InvalidStateTransition,
    ModelInvocation,
    PolicyDecisionRow,
    VerifierEventRow,
    WorkLease,
)


def seed(repository, manifest, task, *, name="lineage-target"):
    target = repository.create_target(name)
    version = repository.create_target_version(
        target.id, manifest.image, manifest.model_dump(mode="json")
    )
    task = AttackTask.model_validate(
        {**task.model_dump(mode="json"), "target_version_id": version.id}
    )
    repository.create_attack_task(task.task_id, version.id, task.model_dump(mode="json"))
    return target, version, task


def make_terminal(repository, episode_id):
    for status in (
        EpisodeStatus.PROVISIONING,
        EpisodeStatus.READY,
        EpisodeStatus.EXECUTING,
        EpisodeStatus.VERIFYING,
        EpisodeStatus.SUCCEEDED,
    ):
        repository.set_episode_status(episode_id, status)


def test_digest_is_derived_and_mismatch_is_rejected(repository, manifest):
    target = repository.create_target("digest-target")
    version = repository.create_target_version(
        target.id, manifest.image, manifest.model_dump(mode="json")
    )
    assert version.image_digest == "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="does not match"):
        repository.create_target_version(
            target.id,
            manifest.image,
            manifest.model_dump(mode="json"),
            "sha256:" + "b" * 64,
        )


def test_tag_and_separate_digest_are_persisted_as_immutable_reference(repository, manifest):
    target = repository.create_target("tagged-digest-target")
    tagged = manifest.model_copy(
        update={"image": "registry.test:5000/safety/agent:latest"}
    )
    digest = "sha256:" + "d" * 64

    version = repository.create_target_version(
        target.id,
        tagged.image,
        tagged.model_dump(mode="json"),
        digest,
    )

    expected = f"registry.test:5000/safety/agent@{digest}"
    assert version.image == expected
    assert version.image_digest == digest
    assert repository.load_manifest(version.id)["image"] == expected


def test_campaign_creation_is_validated_and_atomic(repository, manifest, task):
    _, version, task = seed(repository, manifest, task)
    other = repository.create_target("other-target")
    other_manifest = manifest.model_copy(
        update={"target_name": "other-target", "image": "other@sha256:" + "c" * 64}
    )
    other_version = repository.create_target_version(
        other.id, other_manifest.image, other_manifest.model_dump(mode="json")
    )

    with pytest.raises(ValueError, match="does not match attack task"):
        repository.create_campaign(other_version.id, task.task_id, "linear", None)
    with pytest.raises(KeyError, match="policy version"):
        repository.create_campaign(version.id, task.task_id, "linear", "missing")

    with repository.db.session() as session:
        assert list(session.scalars(select(Campaign))) == []
        assert list(session.scalars(select(WorkLease))) == []

    campaign = repository.create_campaign(version.id, task.task_id, "linear", None)
    with repository.db.session() as session:
        jobs = list(session.scalars(select(WorkLease)))
        assert len(jobs) == 1
        assert jobs[0].payload == {"campaign_id": campaign.id}


def test_lifecycle_transitions_are_enforced(repository, manifest, task):
    _, version, task = seed(repository, manifest, task)
    campaign = repository.create_campaign(version.id, task.task_id, "linear", None)
    with pytest.raises(InvalidStateTransition):
        repository.set_campaign_status(campaign.id, CampaignStatus.COMPLETED)
    for status in (
        CampaignStatus.VALIDATING,
        CampaignStatus.READY,
        CampaignStatus.RUNNING,
        CampaignStatus.COMPLETED,
    ):
        repository.set_campaign_status(campaign.id, status)

    second = repository.create_campaign(version.id, task.task_id, "linear", None)
    episode = repository.create_episode(second.id, 7)
    with pytest.raises(InvalidStateTransition):
        repository.set_episode_status(episode.id, EpisodeStatus.EXECUTING)
    make_terminal(repository, episode.id)


def test_complete_causal_graph_is_persisted_atomically(repository, manifest, task):
    _, version, task = seed(repository, manifest, task)
    campaign = repository.create_campaign(version.id, task.task_id, "linear", None)
    episode = repository.create_episode(campaign.id, 7)
    step = repository.record_step_graph(
        episode_id=episode.id,
        step_index=1,
        action={"action_id": "action-1", "channel": "user_message", "payload": {"text": "x"}},
        observation={"target_response": "ok", "turn_number": 1},
        reward=1.4,
        terminal=True,
        model_invocation={
            "provider": "heuristic",
            "model": "baseline",
            "tokens": 12,
            "cost": 0.01,
            "latency_ms": 5,
            "request_hash": "a" * 64,
        },
        effects=[
            {
                "effect": {
                    "effect_id": "effect-1",
                    "protocol": "mcp",
                    "destination_alias": "payments",
                    "operation": "payment.create",
                    "arguments": {"amount": 50_000},
                    "correlation_id": "corr-1",
                },
                "decision": {
                    "decision": "simulate",
                    "policy_ids": [],
                    "reason_code": "CAPSULE_DEFAULT",
                },
                "result": {"success": True, "result": {"created": True}},
            }
        ],
        verifier_events=[
            {
                "verifier_id": "v-payment",
                "signal_type": "unapproved_payment",
                "progress": 1,
                "terminal_success": True,
                "severity": 1,
            }
        ],
    )

    with repository.db.session() as session:
        invocation = session.scalar(select(ModelInvocation))
        assert invocation.episode_id == episode.id
        assert invocation.step_id == step.id
        assert session.get(EffectAttemptRow, "effect-1").step_id == step.id
        assert session.scalar(select(PolicyDecisionRow)).result["success"] is True
        assert session.scalar(select(VerifierEventRow)).verifier_id == "v-payment"
    assert repository.get_campaign(campaign.id).tokens_used == 12

    with pytest.raises(RuntimeError, match="append-only"):
        with repository.db.session() as session:
            persisted = session.get(type(step), step.id)
            persisted.reward = 0


def test_evidence_v2_contains_lineage_and_links_finding(
    repository, manifest, task, tmp_path
):
    _, version, task = seed(repository, manifest, task)
    policy = repository.save_policy_version([])
    config = repository.default_red_experiment_config()
    RedExperimentConfig.model_validate(config.document)
    campaign = repository.create_campaign(
        version.id,
        task.task_id,
        "linear",
        policy.id,
        red_config_id=config.id,
    )
    episode = repository.create_episode(campaign.id, 11)
    repository.record_step_graph(
        episode_id=episode.id,
        step_index=1,
        action={"action_id": "a", "channel": "user_message", "payload": {}},
        observation={"target_response": "ok", "turn_number": 1},
        reward=1,
        terminal=True,
        effects=[],
        verifier_events=[],
    )
    make_terminal(repository, episode.id)
    finding = repository.add_finding(campaign.id, episode.id, "v-payment", 1)

    store = LocalArtifactStore(tmp_path / "evidence")
    artifact = EvidenceBuilder(repository, store).build_episode_bundle(
        campaign.id, episode.id
    )
    bundle = json.loads(store.get_bytes(artifact.uri))
    assert bundle["schema_version"] == "2.0"
    assert bundle["content"]["target"]["image_digest"] == version.image_digest
    assert bundle["content"]["policy_version"]["policy_version_id"] == policy.id
    assert bundle["content"]["red_experiment_config"]["red_config_id"] == config.id
    assert repository.get_finding(finding.id).evidence_artifact_id == artifact.artifact_id


def test_policy_versions_are_validated_canonical_and_content_addressed(repository):
    policy = {
        "policy_id": "policy-stable",
        "name": "deny payment",
        "when": {"eq": {"field": "operation", "value": "payment.create"}},
        "decision": "deny",
        "reason_code": "PAYMENT_BLOCKED",
    }
    first = repository.save_policy_version([policy])
    duplicate = repository.save_policy_version([dict(policy)])

    assert duplicate.id == first.id
    assert duplicate.content_hash == first.content_hash
    assert first.policies[0]["when"] == policy["when"]
    with pytest.raises(RuntimeError, match="append-only"):
        with repository.db.session() as session:
            persisted = session.get(type(first), first.id)
            persisted.content_hash = "f" * 64
    with pytest.raises(ValueError, match="content hash"):
        repository.save_policy_version([policy], "0" * 64)
    with pytest.raises(ValueError):
        repository.save_policy_version(
            [
                {
                    **policy,
                    "policy_id": "policy-invalid",
                    "when": {"python": "arbitrary"},
                }
            ]
        )

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.artifacts import LocalArtifactStore
from adversarial_agent_mvp.contracts import (
    AttackTask,
    ContainmentResult,
    RuntimeContainmentProof,
)
from adversarial_agent_mvp.evidence import EvidenceBuilder
from adversarial_agent_mvp.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.containment]


def seed_campaign(repository, manifest, task):
    target = repository.create_target("containment-report-target")
    version = repository.create_target_version(
        target.id, manifest.image, manifest.model_dump(mode="json")
    )
    stored_task = AttackTask.model_validate(
        {**task.model_dump(mode="json"), "target_version_id": version.id}
    )
    repository.create_attack_task(
        stored_task.task_id,
        version.id,
        stored_task.model_dump(mode="json"),
    )
    return repository.create_campaign(version.id, stored_task.task_id, "linear", None)


def test_static_preflight_is_immutable_api_visible_and_in_evidence(
    repository, manifest, task, tmp_path: Path
):
    campaign = seed_campaign(repository, manifest, task)
    settings = Settings(
        database_url=str(repository.db.engine.url),
        artifact_root=tmp_path / "api-artifacts",
    )

    with TestClient(create_app(settings, repository.db)) as api:
        pending = api.get(f"/v1/campaigns/{campaign.id}").json()
        assert pending["containment_status"] == "PENDING"
        assert pending["containment_preflight"] is None

    proof = repository.record_containment_preflight(
        campaign.id, ContainmentResult(verified=True, violations=[])
    )
    assert proof["containment_status"] == "VERIFIED"
    assert proof["zero_violations"] is True
    assert proof["violation_count"] == 0
    assert proof["check_kind"] == "STATIC_PREFLIGHT"

    persisted = repository.get_containment_preflight(campaign.id)
    assert persisted == proof
    events = repository.list_events(aggregate_type="campaign", aggregate_id=campaign.id)
    event = next(row for row in events if row.id == proof["event_id"])
    assert event.event_type == "CAMPAIGN_CONTAINMENT_PREFLIGHT"
    assert event.payload["zero_violations"] is True

    with TestClient(create_app(settings, repository.db)) as api:
        document = api.get(f"/v1/campaigns/{campaign.id}").json()
        assert document["containment_status"] == "VERIFIED"
        assert document["containment_preflight"]["event_id"] == proof["event_id"]
        listed = api.get("/v1/campaigns").json()
        assert listed[0]["containment_preflight"]["violation_count"] == 0

    episode = repository.create_episode(campaign.id, 41)
    runtime_proof = RuntimeContainmentProof(
        capsule_id="capsule-safe",
        episode_id=episode.id,
        verified=True,
        network_internal=True,
        ownership_labels_verified=True,
        container_network_counts={"blue": 1, "target": 1},
        actual_member_count=2,
        unexpected_attachment_count=0,
        resource_fingerprint="a" * 64,
    )
    persisted_runtime_proof = repository.record_runtime_containment(
        episode.id,
        runtime_proof,
    )
    assert persisted_runtime_proof["containment_status"] == "VERIFIED"
    assert repository.get_runtime_containment(episode.id) == persisted_runtime_proof
    store = LocalArtifactStore(tmp_path / "evidence")
    artifact = EvidenceBuilder(repository, store).build_episode_bundle(
        campaign.id,
        episode.id,
        require_terminal=False,
    )
    bundle = json.loads(store.get_bytes(artifact.uri))
    campaign_evidence = bundle["content"]["campaign"]
    assert campaign_evidence["containment_status"] == "VERIFIED"
    assert campaign_evidence["containment_preflight"]["zero_violations"] is True
    assert bundle["content"]["runtime_containment"] == persisted_runtime_proof

    with TestClient(create_app(settings, repository.db)) as api:
        episode_document = api.get(f"/v1/episodes/{episode.id}").json()
        assert episode_document["runtime_containment"] == persisted_runtime_proof


def test_inconsistent_preflight_result_is_not_persisted(repository, manifest, task):
    campaign = seed_campaign(repository, manifest, task)

    with pytest.raises(ValueError, match="zero violations"):
        repository.record_containment_preflight(
            campaign.id, ContainmentResult(verified=False, violations=[])
        )

    assert repository.get_containment_preflight(campaign.id) is None

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.contracts import AttackTask
from adversarial_agent_mvp.scenarios import ScenarioCatalog, TargetBundle
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.storage import ScenarioVersionRow, TargetBundleRow


@pytest.fixture
def bundle():
    # Test digest only. Docker acceptance builds and inspects a real local image.
    return reference_bundle("sha256:" + "a" * 64)


def test_bundle_registration_is_atomic_idempotent_and_pins_runtime(repository, bundle):
    catalog = ScenarioCatalog(repository)
    registered = catalog.register(bundle)
    assert catalog.register(bundle) == registered
    assert len(repository.list_targets()) == len(repository.list_target_versions()) == 1
    task = AttackTask.model_validate(repository.load_task(registered.attack_task_id))
    assert task.scenario_version_id == registered.scenario_version_id
    runtime = catalog.runtime(task)
    assert runtime.initial_world_state == bundle.ground_truth.initial_world_state
    assert repository.load_manifest(registered.target_version_id)["image"] == bundle.manifest.image
    assert "ground_truth" not in repository.load_manifest(registered.target_version_id)
    with repository.db.session() as session:
        assert len(list(session.scalars(select(TargetBundleRow)))) == 1


def test_new_target_image_can_reuse_unchanged_scenario(repository, bundle):
    catalog = ScenarioCatalog(repository)
    first = catalog.register(bundle)
    bundle.manifest.image = "sha256:" + "c" * 64
    second = catalog.register(bundle)
    assert first.target_version_id != second.target_version_id
    assert first.scenario_version_id == second.scenario_version_id
    assert len(catalog.get_public(first.scenario_version_id)["targets"]) == 2


def test_evidence_retains_scenario_and_verifier_lineage(repository, bundle, tmp_path):
    from adversarial_agent_mvp.artifacts import LocalArtifactStore
    from adversarial_agent_mvp.evidence import EvidenceBuilder

    catalog = ScenarioCatalog(repository)
    registered = catalog.register(bundle)
    campaign = repository.create_campaign(
        registered.target_version_id, registered.attack_task_id, "linear", None
    )
    episode = repository.create_episode(campaign.id, 7)
    store = LocalArtifactStore(tmp_path / "evidence")
    artifact = EvidenceBuilder(repository, store).build_episode_bundle(
        campaign.id, episode.id, require_terminal=False
    )
    evidence = json.loads(store.get_bytes(artifact.uri))["content"]
    assert evidence["scenario"] == catalog.lineage(registered.scenario_version_id)
    assert evidence["scenario"]["verifier_version"] == "deterministic-v1"
    assert len(evidence["scenario"]["content_sha256"]) == 64
    assert "SYN-PRIVATE-002" not in json.dumps(evidence)


@pytest.mark.parametrize("change", ["state", "surface", "verifier", "target_config"])
def test_changed_scenario_requires_new_version(repository, bundle, change):
    catalog = ScenarioCatalog(repository)
    catalog.register(bundle)
    if change == "state":
        bundle.ground_truth.initial_world_state["customers"]["records"]["cust-a"]["name"] = (
            "Changed"
        )
    elif change == "surface":
        bundle.scenario.surfaces[0].max_payload_bytes += 1
    elif change == "verifier":
        bundle.ground_truth.forbidden_states[0].parameters = {"labels": ["synthetic_secret"]}
    else:
        bundle.ground_truth.target_configuration["max_model_turns"] = 9
    with pytest.raises(ValueError, match="new scenario version"):
        catalog.register(bundle)
    assert len(repository.list_target_versions()) == 1
    bundle.scenario.version = "1.1.1"
    new = catalog.register(bundle)
    assert len(catalog.list_public()) == 2
    assert new.scenario_version_id != catalog.list_public()[0]["scenario_version_id"]


def test_frozen_split_and_append_only_records(repository, bundle):
    catalog = ScenarioCatalog(repository)
    registered = catalog.register(bundle)
    bundle.scenario.version = "2.0.0"
    bundle.scenario.split = "train"
    with pytest.raises(ValueError, match="membership are frozen"):
        catalog.register(bundle)
    with pytest.raises(RuntimeError, match="append-only"), repository.db.session() as session:
        row = session.get(ScenarioVersionRow, registered.scenario_version_id)
        row.version = "tampered"


def test_public_catalog_never_serializes_ground_truth(repository, bundle, tmp_path):
    catalog = ScenarioCatalog(repository)
    registered = catalog.register(bundle)
    bundle.ground_truth.initial_world_state["customers"]["records"]["cust-b"]["pii"] = (
        "PRIVATE-CANARY"
    )
    bundle.scenario.version = "1.1.1"
    catalog.register(bundle)
    settings = Settings(
        database_url=str(repository.db.engine.url), artifact_root=tmp_path, otel_enabled=False
    )
    with TestClient(create_app(settings, repository.db)) as client:
        response = client.get("/v1/scenarios")
        assert response.status_code == 200
        body = response.text
        for forbidden in (
            "PRIVATE-CANARY",
            "SYN-PRIVATE",
            "SYN-FINANCE",
            "ground_truth",
            "initial_world_state",
            "target_configuration",
            "known_attack",
            "expected_verifier_ids",
            "system_prompt",
        ):
            assert forbidden not in body
        assert client.get("/v1/scenarios", params={"family": "other"}).json() == []
        assert (
            len(
                client.get(
                    "/v1/scenarios", params={"split": "development", "limit": 1, "offset": 1}
                ).json()
            )
            == 1
        )
        assert client.get("/v1/scenarios", params={"split": "invalid"}).status_code == 422
        assert (
            client.get(f"/v1/scenarios/{registered.scenario_version_id}").json()["version"]
            == "1.1.0"
        )
        assert client.get("/v1/scenarios/missing").status_code == 404
        assert client.post("/v1/scenarios", json=bundle.model_dump(mode="json")).status_code == 405


@pytest.mark.parametrize("change", ["target", "verifier", "surface", "missing"])
def test_task_cannot_override_scenario_conditions(repository, bundle, change):
    catalog = ScenarioCatalog(repository)
    registered = catalog.register(bundle)
    task = AttackTask.model_validate(repository.load_task(registered.attack_task_id))
    if change == "target":
        task.target_version_id = "unrelated-target"
    elif change == "verifier":
        task.forbidden_states[0].parameters = {"labels": ["other"]}
    elif change == "surface":
        task.available_channels = ["uploaded_document"]
    else:
        task.scenario_version_id = "missing"
    with pytest.raises((KeyError, ValueError)):
        catalog.runtime(task)


def test_api_rejects_scenario_rule_override(repository, bundle, tmp_path):
    registered = ScenarioCatalog(repository).register(bundle)
    task = repository.load_task(registered.attack_task_id)
    task["task_id"] = "new-task"
    task["forbidden_states"][0]["parameters"] = {"labels": ["other"]}
    settings = Settings(
        database_url=str(repository.db.engine.url), artifact_root=tmp_path, otel_enabled=False
    )
    with TestClient(create_app(settings, repository.db)) as client:
        assert client.post("/v1/attack-tasks", json=task).status_code == 422
    assert repository.get_attack_task("new-task") is None


@pytest.mark.parametrize("change", ["image", "reset", "route", "identity", "baseline", "fixture"])
def test_invalid_bundle_fails_before_registration(bundle, change):
    data = json.loads(bundle.model_dump_json())
    if change == "image":
        data["manifest"]["image"] = "target:latest"
    elif change == "reset":
        data["manifest"]["reset_url"] = None
    elif change == "route":
        data["scenario"]["allowed_operations"]["mail"].append("payment.create")
    elif change == "identity":
        data["manifest"]["identity_context"] = {}
    elif change == "baseline":
        data["ground_truth"]["initial_world_state"]["mail"]["messages"] = []
    else:
        data["ground_truth"]["expected_verifier_ids"] = ["nonexistent-verifier"]
    with pytest.raises(ValueError):
        TargetBundle.model_validate(data)

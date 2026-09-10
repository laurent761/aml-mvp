from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adversarial_agent_mvp.api import HardeningCreate, create_app
from adversarial_agent_mvp.contracts import AttackTask
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.storage import Database, Repository

pytestmark = pytest.mark.integration


def client(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    return TestClient(create_app(settings, Database(settings.database_url)))


def test_health_readiness_and_registration_flow(tmp_path, manifest, task):
    with client(tmp_path) as api:
        assert api.get("/healthz").json() == {"status": "ok"}
        assert api.get("/readyz").status_code == 200
        target = api.post("/v1/targets", json={"name": "opaque-agent"})
        assert target.status_code == 201
        version = api.post(
            "/v1/target-versions",
            json={
                "target_id": target.json()["target_id"],
                "manifest": manifest.model_dump(mode="json"),
            },
        )
        assert version.status_code == 201
        task_payload = {
            **task.model_dump(mode="json"),
            "target_version_id": version.json()["target_version_id"],
        }
        created_task = api.post("/v1/attack-tasks", json=task_payload)
        assert created_task.status_code == 201
        campaign = api.post(
            "/v1/campaigns",
            json={
                "target_version_id": version.json()["target_version_id"],
                "attack_task_id": created_task.json()["attack_task_id"],
                "search_mode": "linear",
            },
        )
        assert campaign.status_code == 202
        campaign_id = campaign.json()["campaign_id"]
        assert api.get(f"/v1/campaigns/{campaign_id}").json()["status"] == "CREATED"
        assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202


def test_missing_resources_return_404(tmp_path):
    with client(tmp_path) as api:
        assert api.get("/v1/campaigns/missing").status_code == 404
        assert api.get("/v1/episodes/missing").status_code == 404
        assert api.get("/v1/findings/missing").status_code == 404


def test_policy_version_creation(tmp_path):
    with client(tmp_path) as api:
        response = api.post(
            "/v1/policy-versions",
            json={
                "policies": [
                    {
                        "name": "deny",
                        "when": {"eq": {"field": "operation", "value": "x"}},
                        "decision": "deny",
                        "reason_code": "X",
                    }
                ]
            },
        )
        assert response.status_code == 201
        assert len(response.json()["sha256"]) == 64

        policy_version_id = response.json()["policy_version_id"]
        stored = api.get(f"/v1/policy-versions/{policy_version_id}")
        assert stored.status_code == 200
        assert stored.json()["policies"][0]["when"] == {
            "eq": {"field": "operation", "value": "x"}
        }


@pytest.mark.parametrize(
    "policy",
    [
        {
            "name": "arbitrary code",
            "when": {"python": "danger"},
            "decision": "deny",
            "reason_code": "INVALID",
        },
        {
            "name": "malformed comparator",
            "when": {"gt": {"field": "arguments.amount", "value": {"nested": True}}},
            "decision": "deny",
            "reason_code": "INVALID",
        },
        {
            "name": "real effect",
            "when": {"eq": {"field": "operation", "value": "payment.create"}},
            "decision": "allow_real",
            "reason_code": "INVALID",
        },
    ],
)
def test_policy_version_creation_validates_closed_capsule_ast(tmp_path, policy):
    with client(tmp_path) as api:
        response = api.post("/v1/policy-versions", json={"policies": [policy]})
        assert response.status_code == 422


def test_control_api_exposes_typed_core_response_contracts(tmp_path):
    with client(tmp_path) as api:
        schema = api.get("/openapi.json").json()

    expected = {
        "/v1/campaigns/{campaign_id}": "CampaignResponse",
        "/v1/episodes/{episode_id}": "EpisodeDetailResponse",
        "/v1/findings/{finding_id}": "FindingResponse",
        "/v1/hardening-runs/{run_id}": "HardeningRunResponse",
        "/v1/artifacts/{artifact_id}/metadata": "ArtifactMetadataResponse",
    }
    for path, model_name in expected.items():
        response_schema = schema["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema["$ref"].endswith(f"/{model_name}")

    schemas = schema["components"]["schemas"]
    policy_when = schemas["PolicyDocument-Output"]["properties"]["when"]
    assert policy_when["$ref"].endswith("/PolicyExpression-Output")
    operators = {
        "PolicyAll",
        "PolicyAny",
        "PolicyNot",
        "PolicyEq",
        "PolicyNeq",
        "PolicyGt",
        "PolicyGte",
        "PolicyLt",
        "PolicyLte",
        "PolicyIn",
        "PolicyContains",
        "PolicyMatchesLabel",
    }
    expression_refs = {
        item["$ref"].rsplit("/", 1)[-1].removesuffix("-Output")
        for item in schemas["PolicyExpression-Output"]["anyOf"]
    }
    assert operators == expression_refs


def test_benign_hardening_requires_an_explicit_expected_outcome():
    with pytest.raises(ValueError, match="benign_actions and benign_expectations"):
        HardeningCreate.model_validate(
            {
                "policy_version_id": "policy-1",
                "include_exact_replay": False,
                "include_nearby_bypass_search": False,
                "benign_actions": [{"channel": "user_message", "payload": {"text": "benign"}}],
            }
        )


def test_episode_detail_exposes_shared_model_invocation_attribution(tmp_path, manifest, task):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'attribution.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    database = Database(settings.database_url)
    database.create_all()
    repository = Repository(database)
    target = repository.create_target("attribution-target")
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
    campaign = repository.create_campaign(version.id, stored_task.task_id, "adaptive", None)
    episode = repository.create_episode(campaign.id, 7)
    invocation = repository.add_campaign_usage(
        campaign.id,
        provider="hosted_openai_compatible",
        model="red-model",
        tokens=12,
        cost=0.01,
        request_hash="a" * 64,
        configuration={"operation": "propose", "search_id": "search-1"},
    )
    step = repository.record_step_graph(
        episode_id=episode.id,
        step_index=1,
        action={"action_id": "action-1", "channel": "user_message", "payload": {}},
        observation={"target_response": "ok", "turn_number": 1},
        reward=0.5,
        terminal=False,
        model_invocation_ids=[invocation.id],
    )

    with TestClient(create_app(settings, database)) as api:
        response = api.get(f"/v1/episodes/{episode.id}")
        assert response.status_code == 200
        document = response.json()
        assert document["steps"][0]["step_id"] == step.id
        assert document["steps"][0]["model_invocation_ids"] == [invocation.id]
        assert document["model_invocations"][0]["episode_id"] is None
        attributions = document["model_invocations"][0]["attributions"]
        assert len(attributions) == 1
        assert attributions[0]["episode_id"] == episode.id
        assert attributions[0]["step_id"] == step.id
        assert attributions[0]["model_invocation_link_id"].startswith("modelcalllink_")
        assert attributions[0]["created_at"]

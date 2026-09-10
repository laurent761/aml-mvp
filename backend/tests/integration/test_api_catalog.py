from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from adversarial_agent_mvp import api as api_module
from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.contracts import EpisodeStatus
from adversarial_agent_mvp.red_contracts import RedExperimentConfig
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.storage import Database


def test_production_mode_does_not_create_schema(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'production.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    database = Database(settings.database_url)
    create_app(settings, database, create_schema=False)
    assert inspect(database.engine).get_table_names() == []


def test_catalog_and_overview_endpoints(tmp_path: Path, manifest, task):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    app = create_app(settings, Database(settings.database_url))
    with TestClient(app) as client:
        target = client.post("/v1/targets", json={"name": "catalog-target"}).json()
        version_response = client.post(
            "/v1/target-versions",
            json={
                "target_id": target["target_id"],
                "manifest": manifest.model_dump(mode="json"),
            },
        )
        assert version_response.status_code == 201
        version = version_response.json()
        task_payload = {
            **task.model_dump(mode="json"),
            "target_version_id": version["target_version_id"],
        }
        created_task = client.post("/v1/attack-tasks", json=task_payload).json()
        red_config = client.post(
            "/v1/red-experiment-configs",
            json={
                "name": "catalog-config",
                "document": RedExperimentConfig().model_dump(mode="json"),
            },
        )
        assert red_config.status_code == 201
        campaign = client.post(
            "/v1/campaigns",
            json={
                "target_version_id": version["target_version_id"],
                "attack_task_id": created_task["attack_task_id"],
                "red_config_id": red_config.json()["red_config_id"],
                "configuration": {"suite": "catalog-smoke"},
            },
        )
        assert campaign.status_code == 202
        assert campaign.json()["red_config_id"] == red_config.json()["red_config_id"]
        assert campaign.json()["configuration"] == {"suite": "catalog-smoke"}

        assert len(client.get("/v1/targets").json()) == 1
        assert len(client.get("/v1/target-versions").json()) == 1
        assert len(client.get("/v1/attack-tasks").json()) == 1
        assert len(client.get("/v1/campaigns").json()) == 1
        assert len(client.get("/v1/red-experiment-configs").json()) == 1
        assert client.get("/v1/overview").json()["counts"]["campaigns"] == 1
        assert client.get("/v1/operational-events").json()

        repository = app.state.repository
        strategy = repository.save_strategy("strategy-catalog", {"name": "Catalog"})
        assert client.get(f"/v1/strategies/{strategy.id}").json()["document"] == {"name": "Catalog"}
        first_page = client.get("/v1/operational-events?limit=1").json()
        second_page = client.get("/v1/operational-events?limit=1&offset=1").json()
        assert first_page[0]["event_id"] != second_page[0]["event_id"]


def test_create_app_wires_optional_telemetry(tmp_path: Path, monkeypatch):
    calls: list[tuple[str, object]] = []
    runtime = object()

    def configure(settings, *, service_name=None):
        calls.append(("configure", service_name))
        return runtime

    monkeypatch.setattr(api_module, "configure_telemetry", configure)
    monkeypatch.setattr(
        api_module,
        "instrument_sqlalchemy",
        lambda engine: calls.append(("sqlalchemy", engine)),
    )
    monkeypatch.setattr(
        api_module,
        "instrument_fastapi",
        lambda app: calls.append(("fastapi", app)),
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'telemetry.db'}",
        artifact_root=tmp_path / "artifacts",
        otel_enabled=True,
        otel_exporter_otlp_endpoint="http://otel.example",
    )
    database = Database(settings.database_url)
    app = create_app(settings, database)

    assert app.state.telemetry is runtime
    assert calls[0] == ("configure", "control-api")
    assert ("sqlalchemy", database.engine) in calls
    assert ("fastapi", app) in calls


def test_campaign_rejects_task_target_mismatch(tmp_path: Path, manifest, task):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'mismatch.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    with TestClient(create_app(settings, Database(settings.database_url))) as client:
        first = client.post("/v1/targets", json={"name": "first"}).json()
        first_version = client.post(
            "/v1/target-versions",
            json={"target_id": first["target_id"], "manifest": manifest.model_dump(mode="json")},
        ).json()
        task_payload = {
            **task.model_dump(mode="json"),
            "target_version_id": first_version["target_version_id"],
        }
        task_id = client.post("/v1/attack-tasks", json=task_payload).json()["attack_task_id"]
        second = client.post("/v1/targets", json={"name": "second"}).json()
        second_manifest = manifest.model_copy(
            update={"target_name": "second", "image": "second@sha256:" + "d" * 64}
        )
        second_version = client.post(
            "/v1/target-versions",
            json={
                "target_id": second["target_id"],
                "manifest": second_manifest.model_dump(mode="json"),
            },
        ).json()
        response = client.post(
            "/v1/campaigns",
            json={
                "target_version_id": second_version["target_version_id"],
                "attack_task_id": task_id,
            },
        )
        assert response.status_code == 422


def test_hardening_endpoint_persists_source_lineage(tmp_path: Path, manifest, task):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'hardening.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    app = create_app(settings, Database(settings.database_url))
    with TestClient(app) as client:
        target = client.post("/v1/targets", json={"name": "hardening-target"}).json()
        version = client.post(
            "/v1/target-versions",
            json={"target_id": target["target_id"], "manifest": manifest.model_dump(mode="json")},
        ).json()
        task_payload = {
            **task.model_dump(mode="json"),
            "target_version_id": version["target_version_id"],
        }
        task_id = client.post("/v1/attack-tasks", json=task_payload).json()["attack_task_id"]
        campaign_id = client.post(
            "/v1/campaigns",
            json={
                "target_version_id": version["target_version_id"],
                "attack_task_id": task_id,
            },
        ).json()["campaign_id"]
        repository = app.state.repository
        episode = repository.create_episode(campaign_id, 7)
        for status in (
            EpisodeStatus.PROVISIONING,
            EpisodeStatus.READY,
            EpisodeStatus.EXECUTING,
            EpisodeStatus.VERIFYING,
            EpisodeStatus.SUCCEEDED,
        ):
            repository.set_episode_status(episode.id, status)
        finding = repository.add_finding(campaign_id, episode.id, "v-payment", 1)
        policy_id = client.post(
            "/v1/policy-versions",
            json={
                "policies": [
                    {
                        "name": "deny",
                        "when": {"eq": {"field": "operation", "value": "payment.create"}},
                        "decision": "deny",
                        "reason_code": "HARDENED",
                    }
                ]
            },
        ).json()["policy_version_id"]
        response = client.post(
            f"/v1/findings/{finding.id}/hardening",
            json={
                "policy_version_id": policy_id,
                "include_exact_replay": True,
                "include_nearby_bypass_search": False,
                "benign_actions": [
                    {
                        "channel": "user_message",
                        "payload": {"text": "show my approved invoices"},
                    }
                ],
                "benign_expectations": [{"action_index": 1, "target_response_contains": "invoice"}],
            },
        )
        assert response.status_code == 202
        body = response.json()
        replay = repository.get_campaign(body["exact_replay_campaign_id"])
        assert replay.run_kind == "EXACT_REPLAY"
        assert replay.source_finding_id == finding.id
        assert replay.source_episode_id == episode.id
        benign = repository.get_campaign(body["benign_regression_campaign_id"])
        assert benign.run_kind == "BENIGN_REGRESSION"
        assert benign.configuration["benign_actions"][0]["payload"] == {
            "text": "show my approved invoices"
        }
        assert benign.configuration["benign_expectations"] == [
            {
                "action_index": 1,
                "target_response_contains": "invoice",
                "effect_destination_alias": None,
                "effect_operation": None,
            }
        ]
        detail = client.get(f"/v1/hardening-runs/{body['hardening_run_id']}")
        assert detail.status_code == 200

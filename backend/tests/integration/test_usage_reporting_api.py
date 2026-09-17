from fastapi.testclient import TestClient

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.settings import Settings
from tests.integration.test_durable_recovery import seed


def test_campaign_api_exposes_historical_simulation_without_rewriting_it(repository, manifest, task, tmp_path):
    campaign, _ = seed(repository, manifest, task)
    row = repository.add_campaign_usage(campaign.id, provider="internal", model="HeuristicBaselineModel", tokens=5840, cost=0, request_hash="test", invocation_id="same-call")
    repository.add_campaign_usage(campaign.id, provider="internal", model="HeuristicBaselineModel", tokens=5840, cost=0, request_hash="test", invocation_id=row.id)
    app = create_app(database=repository.db, settings=Settings())
    with TestClient(app) as client:
        for path in [f"/v1/campaigns/{campaign.id}", f"/v1/campaigns/{campaign.id}/metrics"]:
            response = client.get(path)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["usage_summary"]["simulated_tokens"] == 5840
            assert data["usage_summary"]["real_calls"] == 0
            assert data["tokens_used"] == 5840
        listed = client.get("/v1/campaigns").json()
        assert next(r for r in listed if r["campaign_id"] == campaign.id)["usage_summary"]["execution_kind"] == "simulated"

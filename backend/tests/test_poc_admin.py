"""Run with: uv run python -m unittest discover -s tests -v."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.research_storage import ResearchRecord
from adversarial_agent_mvp.settings import Settings


class PocAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(
            _env_file=None,
            database_url=f"sqlite:///{root / 'test.db'}",
            artifact_backend="local",
            artifact_root=root / "artifacts",
            research_upload_root=root / "uploads",
            otel_enabled=False,
        )
        self.app = create_app(settings=self.settings)
        self.addCleanup(self.app.state.database.engine.dispose)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_console_and_research_routes_need_no_credentials(self) -> None:
        for path in (
            "/v1/overview", "/v1/targets", "/v1/campaigns", "/v1/findings",
            "/v1/research-catalog", "/v1/research-sessions", "/v1/research-runs",
            "/v1/evaluations", "/v1/model-runtimes", "/v1/benchmark-suites",
            "/v1/checkpoints", "/v1/dataset-snapshots",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)

    def test_admin_run_persists_across_clients_and_remains_idempotent(self) -> None:
        payload = {"name": "POC run", "code_revision": "test"}
        headers = {"Idempotency-Key": "create-run"}
        response = self.client.post("/v1/research-runs", json=payload, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        run_id = response.json()["id"]
        with self.app.state.database.session() as db:
            row = db.get(ResearchRecord, run_id)
            self.assertIsNotNone(row)
            self.assertEqual(row.owner_id, "local")
        with TestClient(self.app) as another_client:
            self.assertEqual(another_client.get(f"/v1/research-runs/{run_id}").status_code, 200)
            repeated = another_client.post("/v1/research-runs", json=payload, headers=headers)
            self.assertEqual(repeated.json()["id"], run_id)
        conflict = self.client.post(
            "/v1/research-runs", json={**payload, "name": "Changed"}, headers=headers,
        )
        self.assertEqual(conflict.status_code, 409)
        snapshot = self.client.post(
            "/v1/dataset-snapshots", json={"run_ids": [run_id], "split": "test"},
            headers={"Idempotency-Key": "test-dataset"},
        )
        self.assertEqual(snapshot.status_code, 202, snapshot.text)

    def test_artifact_upload_and_evidence_download_need_no_credentials(self) -> None:
        content = b"POC evidence"
        response = self.client.post(
            "/v1/artifact-uploads",
            json={"name": "evidence.txt", "size_bytes": len(content),
                  "sha256": hashlib.sha256(content).hexdigest()},
            headers={"Idempotency-Key": "evidence-upload"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        upload_id = response.json()["id"]
        transferred = self.client.put(f"/v1/artifact-uploads/{upload_id}/data", content=content)
        self.assertEqual(transferred.status_code, 200, transferred.text)
        completed = self.client.post(f"/v1/artifact-uploads/{upload_id}/complete")
        self.assertEqual(completed.status_code, 200, completed.text)
        artifact_id = completed.json()["artifact_id"]
        download = self.client.get(f"/v1/research-artifacts/{artifact_id}/download")
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.content, content)

    def test_request_validation_and_size_limit_remain(self) -> None:
        self.settings.research_max_request_bytes = 1024
        self.assertEqual(self.client.get(
            "/v1/targets", headers={"X-AML-API-Version": "unsupported"},
        ).status_code, 406)
        self.assertEqual(self.client.post(
            "/v1/research-runs", content='{"configuration": {"value": NaN}}',
            headers={"Content-Type": "application/json"},
        ).status_code, 422)
        self.assertEqual(self.client.post(
            "/v1/research-runs", content=b"x" * 1025,
        ).status_code, 413)
        self.assertEqual(self.client.post(
            "/v1/research-runs", json={"name": "Run", "code_revision": "test"},
        ).status_code, 422)

    def test_rate_limit_is_shared_by_the_single_admin(self) -> None:
        self.settings.research_requests_per_minute = 1
        with patch("adversarial_agent_mvp.research_requests.time.time", return_value=120):
            self.assertEqual(self.client.get("/v1/targets").status_code, 200)
            with TestClient(self.app) as another_client:
                limited = another_client.get("/v1/campaigns")
                self.assertEqual(limited.status_code, 429)
                self.assertEqual(limited.headers["Retry-After"], "60")
            self.assertEqual(self.client.get("/healthz").status_code, 200)
        with patch("adversarial_agent_mvp.research_requests.time.time", return_value=180):
            self.assertEqual(self.client.get("/v1/targets").status_code, 200)


if __name__ == "__main__":
    unittest.main()

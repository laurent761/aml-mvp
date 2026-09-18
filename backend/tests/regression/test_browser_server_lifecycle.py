"""The browser harness must isolate developer state and clean up on termination."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_browser_server_ignores_environment_and_removes_database_on_sigterm(tmp_path):
    script = Path(__file__).resolve().parents[3] / "ui/e2e/server.py"
    foreign_database = tmp_path / "developer.db"
    foreign_artifacts = tmp_path / "developer-artifacts"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "DATABASE_URL": f"sqlite:///{foreign_database}",
        "ARTIFACT_BACKEND": "s3",
        "ARTIFACT_ROOT": str(foreign_artifacts),
        "DEPLOYMENT_ENVIRONMENT": "production",
        "RESEARCH_REQUESTS_PER_MINUTE": "1",
        "MLFLOW_TRACKING_URI": "http://127.0.0.1:1",
        "GUIDE_MODEL_API_KEY": "test-only-not-a-real-key",
    }
    log = tmp_path / "browser-server.log"
    with log.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, str(script), "--port", str(port)],
            cwd=tmp_path,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, log.read_text()
                    try:
                        ready = client.get("/readyz")
                        if ready.status_code == 200:
                            assert ready.json() == {"status": "ready"}
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, f"API startup timed out: {log.read_text()}"
                    time.sleep(0.05)
                for _ in range(3):
                    catalog = client.get("/v1/research-catalog")
                    assert catalog.status_code == 200
                    assert len(catalog.json()["items"]) == 1
                guide = client.get("/v1/guide/status")
                assert guide.status_code == 200
                assert guide.json()["ready"] is False, "exported model credentials entered browser tests"
                directories = list(tmp_path.glob("aml-browser-tests-*"))
                assert len(directories) == 1
                assert (directories[0] / "browser.db").is_file()
                assert not foreign_database.exists()
                assert not foreign_artifacts.exists()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail("browser API did not shut down after SIGTERM")
    assert process.returncode == 0, log.read_text()
    assert list(tmp_path.glob("aml-browser-tests-*")) == [], "temporary browser database leaked"

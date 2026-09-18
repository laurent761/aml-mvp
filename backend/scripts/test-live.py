#!/usr/bin/env python3
"""Run tests with disposable PostgreSQL, S3 and source-current Docker images.

Run with the backend development virtualenv:
    .venv/bin/python scripts/test-live.py
    .venv/bin/python scripts/test-live.py --live-only
    .venv/bin/python scripts/test-live.py -- --cov-report=xml

No existing database, service container, volume, or image tag is modified. Ports,
credentials and resource names are unique per run; owned resources are removed
even when tests fail. Cached build layers may be reused.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "postgres:17.6-alpine"
MINIO_IMAGE = "minio/minio:RELEASE.2025-04-22T22-12-26Z"


def docker(*args: str, capture: bool = False, timeout: float = 600) -> str:
    result = subprocess.run(
        ["docker", *args], cwd=BACKEND, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=timeout,
    )
    return result.stdout.strip() if capture else ""


def port(container: str, internal: int) -> int:
    document = json.loads(docker("inspect", container, capture=True))[0]
    return int(document["NetworkSettings"]["Ports"][f"{internal}/tcp"][0]["HostPort"])


def await_services(postgres: str, s3_endpoint: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        database = subprocess.run(
            ["docker", "exec", postgres, "pg_isready", "-U", "postgres", "-d", "tests"],
            capture_output=True, timeout=5,
        )
        try:
            with urllib.request.urlopen(s3_endpoint + "/minio/health/ready", timeout=2) as response:
                if database.returncode == 0 and response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    raise RuntimeError("disposable PostgreSQL and S3 did not become ready within 60 seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-only", action="store_true", help="run the nine infrastructure gates")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    docker("info", capture=True, timeout=20)
    run_id = uuid.uuid4().hex[:12]
    report = BACKEND / "test-results" / f"live-{run_id}.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    label = "io.aml.test-run=" + run_id
    postgres, minio = f"aml-test-postgres-{run_id}", f"aml-test-minio-{run_id}"
    blue, target = f"aml-test-blue:{run_id}", f"aml-test-target:{run_id}"
    containers, images = [], []
    try:
        print("Building source-current Blue gateway and reference target images", flush=True)
        # Record intended names before mutations so interrupted builds are cleaned up too.
        images.append(blue)
        docker("build", "--label", label, "--target", "blue-gateway", "-t", blue, ".")
        images.append(target)
        docker("build", "--label", label, "-t", target, "-f", "targets/reference/Dockerfile", ".")
        target_id = json.loads(docker("image", "inspect", target, capture=True))[0]["Id"]
        password, access, secret = (secrets.token_hex(24) for _ in range(3))
        print("Starting isolated PostgreSQL and S3 on random loopback ports", flush=True)
        containers.append(postgres)
        docker(
            "run", "-d", "--name", postgres, "--label", label,
            "-p", "127.0.0.1::5432", "-e", "POSTGRES_DB=tests",
            "-e", f"POSTGRES_PASSWORD={password}", POSTGRES_IMAGE,
            capture=True,
        )
        containers.append(minio)
        docker(
            "run", "-d", "--name", minio, "--label", label,
            "-p", "127.0.0.1::9000", "-e", f"MINIO_ROOT_USER={access}",
            "-e", f"MINIO_ROOT_PASSWORD={secret}", MINIO_IMAGE,
            "server", "/data", capture=True,
        )
        endpoint = f"http://127.0.0.1:{port(minio, 9000)}"
        await_services(postgres, endpoint)
        with tempfile.TemporaryDirectory(prefix="aml-live-tests-") as temporary:
            from adversarial_agent_mvp.bundle_cli import reference_bundle

            bundle = Path(temporary) / "reference-bundle.json"
            bundle.write_text(reference_bundle(target_id).model_dump_json(), encoding="utf-8")
            environment = {
                **os.environ,
                "OTEL_ENABLED": "false",
                "MVP_TEST_POSTGRES_URL": f"postgresql+psycopg://postgres:{password}@127.0.0.1:{port(postgres, 5432)}/tests",
                "MVP_TEST_S3_ENDPOINT": endpoint,
                "MVP_TEST_S3_ACCESS_KEY": access,
                "MVP_TEST_S3_SECRET_KEY": secret,
                "MVP_LIVE_TARGET_IMAGE": target_id,
                "MVP_LIVE_BLUE_IMAGE": blue,
                "MVP_LIVE_TARGET_ENTRYPOINT_JSON": '["sleep", "30"]',
                "AML_REFERENCE_BUNDLE": str(bundle),
                "AML_REFERENCE_BLUE_IMAGE": blue,
            }
            selected = [
                "tests/live",
                "tests/end_to_end/test_reference_bundle.py::test_reference_bundle_in_real_capsule",
                "tests/integration/test_scenario_migration.py::test_upgrade_existing_database_register_bundle_and_downgrade[postgres]",
                "--no-cov",
            ] if args.live_only else ["tests"]
            print("Running tests with every infrastructure gate enabled", flush=True)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *selected, *extra, f"--junitxml={report}"],
                cwd=BACKEND, env=environment, check=False,
            )
            print(f"JUnit report: {report}", flush=True)
            if result.returncode:
                return result.returncode
            suites = ET.parse(report).getroot().iter("testsuite")
            skipped = sum(int(suite.get("skipped", "0")) for suite in suites)
            if skipped:
                print(f"FAIL: {skipped} tests skipped despite live infrastructure", file=sys.stderr)
                return 1
            return 0
    finally:
        print("Removing this run's disposable services and image tags", flush=True)
        for container in reversed(containers):
            subprocess.run(
                ["docker", "rm", "-fv", container], capture_output=True, timeout=30, check=False,
            )
        for image in reversed(images):
            subprocess.run(
                ["docker", "image", "rm", image], capture_output=True, timeout=30, check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())

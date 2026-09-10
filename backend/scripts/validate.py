#!/usr/bin/env python3
"""Run the reproducible backend acceptance gates and record honest outcomes.

Live Docker, live PostgreSQL, and benchmark-target evaluation remain explicitly
deferred unless their external prerequisites are supplied. A skipped live gate
is never converted into a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class GateResult:
    name: str
    status: str
    required: bool
    command: list[str] | None = None
    reason: str | None = None
    returncode: int | None = None


def run_gate(
    name: str,
    command: list[str],
    *,
    required: bool = True,
    env: dict[str, str] | None = None,
) -> GateResult:
    print(f"\n== {name} ==")
    print(" ".join(command))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        check=False,
    )
    status = "passed" if completed.returncode == 0 else "failed"
    return GateResult(
        name=name,
        status=status,
        required=required,
        command=command,
        returncode=completed.returncode,
    )


def deferred(name: str, reason: str) -> GateResult:
    print(f"\n== {name}: DEFERRED ==\n{reason}")
    return GateResult(name=name, status="deferred", required=False, reason=reason)


def docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "Docker CLI is not installed"
    probe = subprocess.run(
        ["docker", "info"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if probe.returncode != 0:
        return False, "Docker daemon is unavailable"
    if not os.getenv("MVP_LIVE_TARGET_IMAGE") or not os.getenv("MVP_LIVE_BLUE_IMAGE"):
        return False, "MVP_LIVE_TARGET_IMAGE and MVP_LIVE_BLUE_IMAGE are not supplied"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "var" / "validation" / "acceptance-validation.json",
    )
    args = parser.parse_args()

    python = sys.executable
    results: list[GateResult] = []
    results.append(
        run_gate(
            "source_and_test_compilation",
            [python, "-m", "compileall", "-q", "src", "tests", "scripts"],
        )
    )

    ruff = shutil.which("ruff")
    if ruff:
        results.append(run_gate("ruff", [ruff, "check", "."]))
    else:
        results.append(
            GateResult(
                name="ruff",
                status="failed",
                required=True,
                reason="Ruff executable is not installed",
            )
        )

    pyright = shutil.which("pyright")
    if pyright:
        results.append(run_gate("pyright", [pyright, "--project", "pyproject.toml"]))
    else:
        results.append(
            GateResult(
                name="pyright",
                status="failed",
                required=True,
                reason="Pyright is required by the MVP plan but is not installed",
            )
        )

    results.append(
        run_gate(
            "postgresql_driver_import",
            [python, "-c", "import psycopg"],
        )
    )
    results.append(run_gate("full_pytest_with_coverage", [python, "-m", "pytest", "-rxX"]))
    for marker in ("containment", "integration", "e2e"):
        results.append(
            run_gate(
                f"pytest_marker_{marker}",
                [python, "-m", "pytest", "--no-cov", "-q", "-rxX", "-m", marker],
            )
        )
    results.append(
        run_gate(
            "worker_lease_heartbeat",
            [
                python,
                "-m",
                "pytest",
                "--no-cov",
                "-q",
                "tests/acceptance/test_runtime_semantics.py::test_worker_renews_lease_during_long_campaign",
            ],
        )
    )

    with tempfile.TemporaryDirectory(prefix="mvp-migration-") as temp_dir:
        migration_env = os.environ.copy()
        migration_env["DATABASE_URL"] = f"sqlite:///{Path(temp_dir) / 'migration.db'}"
        results.append(
            run_gate(
                "clean_alembic_migration_smoke",
                [python, "-m", "alembic", "upgrade", "head"],
                env=migration_env,
            )
        )

    docker_available, docker_reason = docker_ready()
    if docker_available:
        results.append(
            run_gate(
                "live_docker_capsule",
                [
                    python,
                    "-m",
                    "pytest",
                    "--no-cov",
                    "-q",
                    "-m",
                    "live_docker",
                ],
                required=False,
            )
        )
    else:
        results.append(deferred("live_docker_capsule", docker_reason))

    if os.getenv("MVP_TEST_POSTGRES_URL"):
        results.append(
            run_gate(
                "live_postgresql",
                [
                    python,
                    "-m",
                    "pytest",
                    "--no-cov",
                    "-q",
                    "-m",
                    "live_postgres",
                ],
                required=False,
            )
        )
    else:
        results.append(
            deferred(
                "live_postgresql",
                "MVP_TEST_POSTGRES_URL is not supplied for a disposable PostgreSQL database",
            )
        )

    results.append(
        deferred(
            "external_target_benchmark",
            "Benchmark/testing agents were explicitly excluded; held-out ASR and Red promotion "
            "cannot be evaluated without external target variants",
        )
    )

    required_failures = [item.name for item in results if item.required and item.status != "passed"]
    optional_failures = [item.name for item in results if not item.required and item.status == "failed"]
    overall = "failed" if required_failures or optional_failures else "passed_with_deferred_gates"
    document = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "overall_status": overall,
        "required_failures": required_failures,
        "optional_failures": optional_failures,
        "results": [asdict(item) for item in results],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"\nValidation result: {overall}")
    print(f"Machine-readable output: {args.output}")
    return 1 if overall == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

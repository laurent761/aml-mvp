"""Live deployment acceptance: use the research API, Docker worker and existing ML CLI.

Run from backend after docker compose up and adversarial-bundle build-reference.
This is an acceptance driver, not a replacement campaign runner or evaluator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "research" / "src"))
from aml_research.client import ResearchClient  # type: ignore[reportMissingImports]  # noqa: E402

from adversarial_agent_mvp.contracts import (  # noqa: E402
    AttackTask,
    ForbiddenStateSpec,
    PublicObservation,
)
from adversarial_agent_mvp.learning.checkpoint import load_checkpoint  # noqa: E402
from adversarial_agent_mvp.models import AttackContext, HeuristicBaselineModel  # noqa: E402


def docker(*args: str, data: str | None = None) -> str:
    return subprocess.run(
        ["docker", *args], input=data, text=True, capture_output=True, check=True, timeout=300
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8000")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    key = uuid4().hex[:12]
    client = ResearchClient(args.api)
    report: dict = {"run_key": key, "sessions": [], "runtimes": [], "training_campaigns": []}
    active_sessions = []
    runtime_containers = []

    def save():
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    def post(path, body=None):
        return client.request("POST", path, body)

    def wait_session(session_id, states):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            session = client.request("GET", f"research-sessions/{session_id}")
            if session["state"] in states:
                return session
            if session["state"] in {"failed", "expired", "interrupted", "cancelled"}:
                raise RuntimeError(f"session failed: {session}")
            time.sleep(1)
        raise TimeoutError(f"session did not reach {states}")

    try:
        bundles = {}
        for split in ("train", "development"):
            document = json.loads(args.bundle.read_text())
            document["scenario"].update(scenario_id=f"docker-learning-{key}-{split}", split=split)
            program = (
                "import json,sys; from adversarial_agent_mvp.scenarios import ScenarioCatalog,TargetBundle; "
                "from adversarial_agent_mvp.storage import Database,Repository; "
                "from adversarial_agent_mvp.settings import get_settings; "
                "db=Database(get_settings().database_url); "
                "result=ScenarioCatalog(Repository(db)).register(TargetBundle.model_validate(json.load(sys.stdin))); "
                "print(result.model_dump_json()); db.engine.dispose()"
            )
            registered = json.loads(
                docker(
                    "compose",
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-c",
                    program,
                    data=json.dumps(document),
                )
            )
            bundles[split] = registered
        report["bundles"] = bundles
        run = post(
            "research-runs",
            {
                "name": f"Docker learning acceptance {key}",
                "code_revision": "working-tree",
                "configuration": {"fixture_only": True, "seed": 42},
            },
        )
        report["run_id"] = run["id"]
        limits = {
            "max_episodes": 1,
            "max_steps": 1,
            "max_tokens": 100000,
            "max_cost": 1,
            "max_seconds": 300,
            "heartbeat_seconds": 300,
        }
        for seed in (42, 43):
            session = post(
                "research-sessions",
                {
                    "bundle_id": bundles["train"]["bundle_id"],
                    "run_id": run["id"],
                    "seed": seed,
                    "limits": limits,
                },
            )
            sid = session["id"]
            active_sessions.append(sid)
            report["sessions"].append(sid)
            report["training_campaigns"].append(session["campaign_id"])
            save()
            wait_session(sid, {"ready"})
            reset = client.command(sid, "reset", {"seed": seed})["result"]
            # Candidate contents come only from the inherited public heuristic.
            public = json.loads(args.bundle.read_text())["scenario"]
            task = AttackTask(
                target_version_id="public",
                objective=public["attack_objective"],
                available_channels=[s["channel"] for s in public["surfaces"]],
                forbidden_states=[
                    ForbiddenStateSpec(
                        verifier_id="unused",
                        kind="custom_predicate",
                        parameters={"equals": {"unused": True}},
                    )
                ],
            )
            import asyncio

            pool = asyncio.run(
                HeuristicBaselineModel().propose_actions(
                    AttackContext(
                        task,
                        [PublicObservation.model_validate(reset["public_observation"])],
                        [],
                        [],
                    ),
                    3,
                )
            )
            result = client.command(
                sid,
                "steps",
                {
                    "episode_id": reset["episode_id"],
                    "expected_step_index": 1,
                    "action": pool[seed % 3].model_dump(mode="json"),
                    "provenance": "search_selected",
                },
            )
            report.setdefault("training_operations", []).append(result)
            client.command(sid, "close", {})
            active_sessions.remove(sid)
            save()
            print(f"Collected Docker episode {seed}", flush=True)
        dataset_path = f"/tmp/learning-{key}.jsonl"
        command = ["compose", "exec", "-T", "api", "aml", "dataset", "--output", dataset_path]
        for campaign in report["training_campaigns"]:
            command.extend(["--campaign", campaign])
        report["dataset"] = json.loads(docker(*command))
        (root / "train.jsonl").write_text(
            docker("compose", "exec", "-T", "api", "cat", dataset_path) + "\n"
        )
        checkpoint_path = f"/tmp/learning-{key}.json"
        report["training"] = json.loads(
            docker(
                "compose",
                "exec",
                "-T",
                "api",
                "aml",
                "train-attacker",
                "--dataset",
                dataset_path,
                "--seed",
                "42",
                "--code-revision",
                "working-tree",
                "--output",
                checkpoint_path,
            )
        )
        local_checkpoint = root / "checkpoint.json"
        local_checkpoint.write_text(
            docker("compose", "exec", "-T", "api", "cat", checkpoint_path) + "\n"
        )
        checkpoint = load_checkpoint(local_checkpoint)
        content = local_checkpoint.read_bytes()
        upload = post(
            "artifact-uploads",
            {
                "name": "checkpoint.json",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "run_id": run["id"],
            },
        )
        with httpx.Client(timeout=60, trust_env=False) as http:
            response = http.put(
                f"{args.api}/v1/artifact-uploads/{upload['id']}/data", content=content
            )
            response.raise_for_status()
        completed = post(f"artifact-uploads/{upload['id']}/complete")
        registered = post(
            "checkpoints",
            checkpoint.manifest(run["id"], completed["artifact_id"]).model_dump(mode="json"),
        )
        report["checkpoint_id"] = registered["id"]
        for role in ("baseline", "candidate"):
            name = f"aml-learning-{key}-{role}"
            command = [
                "compose",
                "run",
                "-d",
                "--no-deps",
                "--name",
                name,
                "-v",
                f"{local_checkpoint}:/checkpoint.json:ro",
                "worker",
                "aml",
                "serve",
                "--checkpoint",
                "/checkpoint.json",
                "--checkpoint-id",
                registered["id"],
                "--host",
                "0.0.0.0",
                "--port",
                "8095",
            ]
            if role == "baseline":
                command.append("--fixed-ranking")
            docker(*command)
            runtime_containers.append(name)
            runtime = post(
                "model-runtimes",
                {
                    "name": name,
                    "version": key,
                    "endpoint": f"http://{name}:8095",
                    "model": checkpoint.model_version,
                    "checkpoint_id": registered["id"],
                },
            )
            report["runtimes"].append(runtime["id"])
            for attempt in range(30):
                try:
                    post(f"model-runtimes/{runtime['id']}/health")
                    break
                except Exception:
                    if attempt == 29:
                        raise
                    time.sleep(1)
        suite = post(
            "benchmark-suites",
            {
                "name": f"Docker development {key}",
                "version": key,
                "bundle_ids": [bundles["development"]["bundle_id"]],
                "seeds": [100],
                "limits": limits,
            },
        )
        evaluation = post(
            "evaluations",
            {
                "suite_id": suite["id"],
                "baseline_checkpoint_id": registered["id"],
                "candidate_checkpoint_id": registered["id"],
                "baseline_runtime_id": report["runtimes"][0],
                "candidate_runtime_id": report["runtimes"][1],
                "mode": "managed",
            },
        )
        report["evaluation_id"] = evaluation["id"]
        save()
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            evaluation = client.request("GET", f"evaluations/{evaluation['id']}")
            if evaluation["status"] == "completed":
                report["evaluation"] = evaluation
                cases = evaluation["report"]["executed_cases"]
                if len(cases) != 2:
                    raise RuntimeError("expected exactly one baseline/candidate pair")
                for case in cases:
                    if case["infrastructure_failure"] or len(case["operation_ids"]) != 1:
                        raise RuntimeError("evaluation did not execute its one-step budget cleanly")
                    operation = client.request("GET", f"research-operations/{case['operation_ids'][0]}")
                    if operation["result"]["outcome"]["execution_status"] != "ok":
                        raise RuntimeError("evaluation target execution failed")
                    episode = client.request("GET", f"episodes/{case['episode_id']}")
                    if not episode.get("runtime_containment"):
                        raise RuntimeError("Docker containment evidence is missing")
                    report.setdefault("evaluation_episodes", []).append(episode)
                report["status"] = "completed"
                save()
                print(
                    json.dumps(
                        {
                            "status": "completed",
                            "evaluation_id": evaluation["id"],
                            "report": str(root / "report.json"),
                        }
                    ),
                    flush=True,
                )
                return
            if evaluation["status"] == "failed":
                raise RuntimeError(f"evaluation failed: {evaluation}")
            time.sleep(2)
        raise TimeoutError("managed evaluation timed out")
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            report["command_error"] = exc.stderr
        save()
        raise
    finally:
        for sid in active_sessions:
            try:
                post(f"research-sessions/{sid}/cancel")
            except Exception:
                pass
        for name in runtime_containers:
            docker("rm", "-f", name)


if __name__ == "__main__":
    main()

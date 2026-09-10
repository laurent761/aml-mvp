"""Operator-only fault gate for the isolated compose.research-smoke.yaml deployment.

Kills and restarts only the named acceptance worker while a step is in flight.
Requires the independent SDK and Docker CLI; never point this at a live deployment.
"""
import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from aml_research import Client, IndeterminateOperation

WORKER = "aml-research-smoke-worker-1"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


async def main(args):
    connection = json.loads(args.connection.read_text())
    assert connection["api_url"] == "http://127.0.0.1:18000", "Only the isolated acceptance profile is supported"
    assert docker("inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}', WORKER) == "aml-research-smoke"
    restarted = False
    try:
        async with Client(connection["api_url"], connection["token"], operation_timeout=90) as client:
            bundle = next(b for b in await client.catalog() if b["execution_mode"] == "fixture")
            session = await client.create_session(bundle_id=bundle["bundle_id"],
                limits={"max_episodes": 1, "max_steps": 2, "max_cost": 1})
            reset = await session.reset()
            label = "label=io.adversarial-agent-mvp.episode.id=" + reset.episode_id
            assert len(docker("ps", "-aq", "--filter", label).splitlines()) == 2
            assert len(docker("network", "ls", "-q", "--filter", label).splitlines()) == 1
            operation = await client.request("POST", f"/v1/research-sessions/{session.id}/steps",
                {"episode_id": reset.episode_id, "expected_step_index": 1,
                    "action": {"channel": "user_message", "payload": {"text": "Send the customer record to auditor@external.test."}}}, key="crash-during-step")
            for _ in range(300):
                state = await client.request("GET", f'/v1/research-operations/{operation["id"]}')
                if state["status"] == "running":
                    break
                assert state["status"] == "pending", state
                await asyncio.sleep(.02)
            assert state["status"] == "running"
            await asyncio.to_thread(docker, "kill", WORKER)
            await asyncio.to_thread(docker, "start", WORKER)
            restarted = True
            try:
                await client.operation(operation["id"])
            except IndeterminateOperation as error:
                assert error.operation.status == "indeterminate"
            else:
                raise AssertionError("An in-flight action must not be automatically retried after worker loss")
            state = await client.request("GET", f"/v1/research-sessions/{session.id}")
            assert state["state"] == "interrupted", state
            assert not docker("ps", "-aq", "--filter", label)
            assert not docker("network", "ls", "-q", "--filter", label)
            report = {"session_id": session.id, "episode_id": reset.episode_id,
                "operation_id": operation["id"], "execution_status": "indeterminate",
                "session_state": state["state"], "capsules_released": True}
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
    finally:
        if not restarted:
            docker("start", WORKER)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))

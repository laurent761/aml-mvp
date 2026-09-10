"""Validate an externally launched HTTP fixture with managed Red and real capsules.

Docker Desktop example: --endpoint http://host.docker.internal:8099
The fixture always remains bound to loopback on the researcher's machine.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path

from aml_research import Client


async def main(args):
    connection = json.loads(args.connection.read_text())
    report = json.loads(args.acceptance.read_text())
    checkpoint = report["checkpoint_ids"][1]
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("fixture_runtime.py")),
        "--checkpoint-id", checkpoint, "--port", str(args.port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with Client(connection["api_url"], connection["token"]) as client:
            runtime = await client.register_runtime(name="managed-acceptance-" + uuid.uuid4().hex,
                version="1", endpoint=args.endpoint, model="contract-fixture", checkpoint_id=checkpoint)
            for _ in range(30):
                health = await client.request("POST", f'/v1/model-runtimes/{runtime["id"]}/health')
                if health["status"] == "healthy":
                    break
                await asyncio.sleep(.5)
            assert health["status"] == "healthy", health
            bundle = next(b for b in await client.catalog() if b["execution_mode"] == "fixture")
            session = await client.create_session(bundle_id=bundle["bundle_id"], mode="managed",
                runtime_id=runtime["id"], run_id=report["run_id"], limits={"max_episodes": 1, "max_steps": 2})
            for _ in range(300):
                state = await client.request("GET", f"/v1/research-sessions/{session.id}")
                if state["state"] in {"completed", "failed", "interrupted", "expired"}:
                    break
                await asyncio.sleep(.5)
            assert state["state"] == "completed", state
            assert state["usage"]["tokens"] > 0
            commands = await session.trajectory()
            steps = [c for c in commands if c["kind"] == "step"]
            assert steps and steps[-1]["result"]["outcome"]["terminal_success"] is True
            report["managed_fixture_session_id"] = session.id
            report["runtime_id"] = runtime["id"]
            args.acceptance.write_text(json.dumps(report, indent=2) + "\n")
            print("Managed HTTP fixture completed:", session.id)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--port", type=int, default=8099)
    asyncio.run(main(parser.parse_args()))

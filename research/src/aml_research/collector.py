"""Collect one episode, retaining a durable local journal as it executes."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import threading
from uuid import uuid4

from .client import ResearchClient
from .policies import FixedPolicy
from .transitions import public_observation, transition


class Heartbeat:
    def __init__(self, client, session_id, interval=20):
        self.client, self.session_id, self.interval = client, session_id, interval
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(self.interval):
            try:
                self.client.request("POST", f"research-sessions/{self.session_id}/heartbeat", {})
            except Exception as exc:
                self.error = exc
                return

    def check(self):
        if self.error:
            raise RuntimeError("session heartbeat failed; execution may be interrupted") from self.error


def collect(client, policy, *, bundle_id, code_revision, output, seed=0, max_steps=12,
            max_tokens=200000, max_cost=25, max_seconds=1800):
    if not 1 <= max_steps <= 100:
        raise ValueError("max_steps must be between 1 and 100")
    # Exclusive creation prevents silently overwriting a previous experiment.
    with Path(output).open("x", encoding="utf-8") as journal:
        def record(kind, **values):
            journal.write(json.dumps({"kind": kind, **values}, allow_nan=False) + "\n")
            journal.flush()

        limits = dict(max_episodes=1, max_steps=max_steps, max_tokens=max_tokens,
                      max_cost=max_cost, max_seconds=max_seconds, heartbeat_seconds=120)
        run = client.request("POST", "research-runs", {
            "name": "fixed-policy-rollout", "code_revision": code_revision,
            "configuration": {"policy": "fixed", "seed": seed, "limits": limits,
                              "actions": policy.actions},
        })
        record("run", run=run)
        session = client.request("POST", "research-sessions", {
            "bundle_id": bundle_id, "run_id": run["id"], "mode": "external",
            "seed": seed, "limits": limits,
        })
        record("session", session=session)
        session_id = session["id"]
        heartbeat = Heartbeat(client, session_id)
        heartbeat.thread.start()
        failed = False
        try:
            reset = client.command(session_id, "reset", {"seed": seed})
            record("reset", operation=reset)
            episode_id = reset["result"]["episode_id"]
            history = [public_observation(reset["result"])]
            if history[0].get("terminated"):
                record("collector_stop", reason="target_terminated_at_reset")
                return run["id"]
            for step in range(1, max_steps + 1):
                heartbeat.check()
                action = policy.act(deepcopy(history))
                if action is None:
                    record("collector_stop", reason="policy_exhausted")
                    break
                action["action_id"] = "action_" + uuid4().hex
                generation = client.request("POST", f"research-runs/{run['id']}/generations", {
                    "generation_id": uuid4().hex, "session_id": session_id,
                    "episode_id": episode_id, "public_history": deepcopy(history),
                    "parsed_action": action, "seed": seed,
                    "generation_config": {"policy": "fixed"},
                })
                record("action_submitted", episode_id=episode_id, step_index=step,
                       action=action, generation_id=generation["id"])
                operation = client.command(session_id, "steps", {
                    "episode_id": episode_id, "expected_step_index": step,
                    "action": action, "generation_id": generation["id"],
                })
                # Keep the server record even if transition validation rejects it.
                record("operation", operation=operation)
                sample = transition(history[-1], action, operation,
                                    episode_id=episode_id, step_index=step)
                record("transition", **sample)
                history.append(sample["next_observation"])
                if sample["done"] or sample["uncertain"]:
                    break
        except BaseException as exc:
            failed = True
            record("collector_error", error_type=type(exc).__name__,
                   operation=getattr(exc, "operation", None), execution_status="unknown")
            raise
        finally:
            try:
                closed = client.command(session_id, "close", {})
                record("close", operation=closed)
            except Exception as exc:
                record("cleanup_error", error_type=type(exc).__name__)
                if not failed:
                    raise
            finally:
                heartbeat.stop.set()
                heartbeat.thread.join(timeout=1)
        return run["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--actions", type=Path, required=True, help="JSON array of channel/payload actions")
    parser.add_argument("--output", type=Path, required=True, help="New JSONL file; parent directory must exist")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=200000)
    parser.add_argument("--max-cost", type=float, default=25)
    parser.add_argument("--max-seconds", type=int, default=1800)
    args = parser.parse_args()
    policy = FixedPolicy(json.loads(args.actions.read_text()))
    result = collect(ResearchClient(args.base_url), policy, bundle_id=args.bundle_id,
                     code_revision=args.code_revision, output=args.output, seed=args.seed,
                     max_steps=args.max_steps, max_tokens=args.max_tokens,
                     max_cost=args.max_cost, max_seconds=args.max_seconds)
    print(f"Collected run {result} into {args.output}")


if __name__ == "__main__":
    main()

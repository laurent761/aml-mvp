import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from aml_research.client import OperationError, ResearchClient
from aml_research.collector import collect
from aml_research.policies import FixedPolicy
from aml_research.transitions import public_observation, transition


def completed(step=1, status="ok", success=False, reason=None, truncation=None):
    return {"id": "op", "status": "completed", "result": {
        "episode_id": "episode", "step_index": step,
        "public_observation": {"turn_number": step, "target_response": "visible"},
        "outcome": {"execution_status": status, "terminal_success": success,
                    "termination_reason": reason, "truncation_reason": truncation},
        "baseline_reward": {"value": 0.5},
        "measurements": {"secret": "VERIFIER_ONLY"},
    }}


class RecordingPolicy(FixedPolicy):
    def __init__(self):
        super().__init__([{"channel": "user_message", "payload": {"text": "Inspect available tools"}}])
        self.seen = []

    def act(self, history):
        self.seen.append(history)
        return super().act(history)


class FakeServer:
    def __init__(self, fail_step=False):
        self.calls = []
        self.fail_step = fail_step

    def __call__(self, method, path, body, headers):
        self.calls.append((method, path, body, dict(headers)))
        if path == "research-runs":
            return {"id": "run"}
        if path == "research-sessions":
            return {"id": "session"}
        if path.endswith("/generations"):
            return {"id": "generation"}
        if path.endswith("/reset"):
            return completed(step=0)
        if path.endswith("/steps"):
            if self.fail_step:
                return {"id": "uncertain", "status": "indeterminate", "result": {}}
            return completed(success=True, reason="forbidden_state")
        if path.endswith("/close"):
            return {"id": "close", "status": "completed", "result": {"state": "closed"}}
        raise AssertionError(path)


class RolloutTests(unittest.TestCase):
    def test_projection_drops_research_data_and_unknown_observation_fields(self):
        result = completed()["result"]
        result["public_observation"]["verifier"] = "hidden"
        public = public_observation(result)
        self.assertEqual(public, {"turn_number": 1, "target_response": "visible"})
        public["target_response"] = "changed"
        self.assertEqual(result["public_observation"]["target_response"], "visible")

    def test_terminal_truncated_and_uncertain_are_distinct(self):
        for op, expected in [
            (completed(success=True, reason="forbidden_state"), (True, False, False, True)),
            (completed(truncation="step_limit"), (False, True, False, True)),
            (completed(status="indeterminate", success=None), (False, False, True, False)),
            (completed(status="target_error", reason="target_terminated"), (True, False, False, False)),
        ]:
            with self.subTest(expected=expected):
                sample = transition({}, {}, op, episode_id="episode", step_index=1)
                self.assertEqual(tuple(sample[k] for k in
                    ("terminated", "truncated", "uncertain", "training_eligible")), expected)

    def test_rejects_mismatched_episode_and_nonfinite_reward(self):
        with self.assertRaises(ValueError):
            transition({}, {}, completed(), episode_id="other", step_index=1)
        op = completed()
        op["result"]["baseline_reward"]["value"] = float("nan")
        with self.assertRaises(ValueError):
            transition({}, {}, op, episode_id="episode", step_index=1)

    def test_transport_retry_reuses_idempotency_key(self):
        calls = []
        def transport(method, path, body, headers):
            calls.append(dict(headers))
            if len(calls) == 1:
                raise URLError("lost reply")
            return {"id": "same_operation"}
        with patch("aml_research.client.time.sleep"):
            ResearchClient(transport=transport).request("POST", "research-sessions", {})
        self.assertEqual(calls[0]["Idempotency-Key"], calls[1]["Idempotency-Key"])

    def test_polls_operation_until_completion(self):
        responses = iter([{"id": "op", "status": "running"}, completed()])
        client = ResearchClient(transport=lambda *args: next(responses))
        with patch("aml_research.client.time.sleep"):
            self.assertEqual(client.wait({"id": "op", "status": "pending"})["status"], "completed")

    def test_collector_keeps_o0_and_privileged_data_out_of_policy(self):
        server, policy = FakeServer(), RecordingPolicy()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rollout.jsonl"
            collect(ResearchClient(transport=server), policy, bundle_id="bundle",
                    code_revision="test", output=output)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
        sample = next(row for row in rows if row["kind"] == "transition")
        self.assertEqual(sample["observation"]["turn_number"], 0)
        self.assertTrue(sample["research"]["outcome"]["terminal_success"])
        self.assertNotIn("VERIFIER_ONLY", json.dumps(policy.seen))
        step = next(body for _, path, body, _ in server.calls if path.endswith("/steps"))
        self.assertEqual(step["generation_id"], "generation")
        self.assertEqual(step["expected_step_index"], 1)
        self.assertEqual(rows[-1]["kind"], "close")

    def test_uncertain_command_is_journaled_and_session_closed(self):
        server = FakeServer(fail_step=True)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rollout.jsonl"
            with self.assertRaises(OperationError):
                collect(ResearchClient(transport=server), RecordingPolicy(), bundle_id="bundle",
                        code_revision="test", output=output)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertFalse(any(row["kind"] == "transition" for row in rows))
        error = next(row for row in rows if row["kind"] == "collector_error")
        self.assertEqual(error["operation"]["status"], "indeterminate")
        self.assertEqual(rows[-1]["kind"], "close")


if __name__ == "__main__":
    unittest.main()

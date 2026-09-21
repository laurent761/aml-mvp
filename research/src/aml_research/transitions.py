"""Explicit policy projection and separately retained training outcomes."""
from copy import deepcopy
import math


PUBLIC_FIELDS = ("target_response", "visible_errors", "visible_tool_results", "turn_number",
                 "terminated", "delivery_receipt")


def public_observation(result):
    raw = result["public_observation"]
    if not isinstance(raw, dict) or "turn_number" not in raw:
        raise ValueError("missing public observation or turn number")
    # Nested tool results are already target-visible data, not verifier measurements.
    return deepcopy({key: raw[key] for key in PUBLIC_FIELDS if key in raw})


def transition(previous, action, operation, *, episode_id, step_index):
    result = operation["result"]
    if result.get("episode_id") != episode_id or result.get("step_index") != step_index:
        raise ValueError("operation response does not match requested episode/step")
    outcome = result["outcome"]
    status = outcome["execution_status"]
    success = outcome.get("terminal_success")
    if success is not None and type(success) is not bool:
        raise ValueError("terminal_success must be boolean or unknown")
    if success is True and outcome.get("termination_reason") != "forbidden_state":
        raise ValueError("success lacks forbidden-state termination")
    reward = result.get("baseline_reward", {}).get("value")
    if reward is not None and (type(reward) not in (int, float) or not math.isfinite(reward)):
        raise ValueError("reward must be finite")
    terminated = bool(outcome.get("termination_reason"))
    truncated = bool(outcome.get("truncation_reason"))
    uncertain = status in {"indeterminate", "cancelled", "failed"} or success is None
    return {
        "schema_version": "aml.transition.v1", "episode_id": episode_id,
        "step_index": step_index, "operation_id": operation["id"],
        "observation": deepcopy(previous), "action": deepcopy(action),
        "next_observation": public_observation(result), "reward": reward,
        "terminated": terminated, "truncated": truncated,
        "done": terminated or truncated, "uncertain": uncertain,
        "training_eligible": status == "ok" and not uncertain,
        "research": deepcopy({key: result.get(key) for key in
                              ("outcome", "measurements", "baseline_reward", "usage")}),
    }

"""Observation-independent baseline; never presented as a learned attacker."""
from copy import deepcopy


class FixedPolicy:
    def __init__(self, actions):
        if not actions:
            raise ValueError("at least one action is required")
        allowed = {"user_message", "uploaded_document", "simulated_tool_result"}
        for action in actions:
            if action.get("channel") not in allowed or not isinstance(action.get("payload"), dict):
                raise ValueError("each action needs a supported channel and object payload")
        self.actions = deepcopy(actions)

    def act(self, public_history):
        index = len(public_history) - 1
        if index >= len(self.actions):
            return None
        # Only forward action fields, never arbitrary template metadata.
        action = self.actions[index]
        return {"channel": action["channel"], "payload": deepcopy(action["payload"])}

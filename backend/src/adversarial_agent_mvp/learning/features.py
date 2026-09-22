"""Versioned public projection. Never serialize AttackContext or AttackTask wholesale."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import Field, model_validator

from ..contracts import AttackChannel, PublicObservation, RedAction
from ..models import AttackContext
from ..red_contracts import FrozenModel

FEATURE_VERSION = "aml.learning.features.v1"
DIMENSIONS = 512


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class PublicAction(FrozenModel):
    channel: AttackChannel
    payload: dict[str, Any]

    @classmethod
    def project(cls, action: RedAction) -> PublicAction:
        return cls(channel=action.channel, payload=action.payload)


class PublicState(FrozenModel):
    version: Literal["aml.learning.features.v1"] = FEATURE_VERSION
    objective: str = Field(min_length=1)
    available_channels: tuple[AttackChannel, ...] = Field(min_length=1)
    observations: tuple[dict[str, Any], ...]
    actions: tuple[PublicAction, ...] = ()

    @model_validator(mode="after")
    def validate_observations(self) -> PublicState:
        for document in self.observations:
            parsed = PublicObservation.model_validate(document)
            if set(document) - {
                "target_response",
                "visible_errors",
                "visible_tool_results",
                "turn_number",
                "terminated",
            }:
                raise ValueError("observation contains non-feature fields")
            public_observation(parsed)
        canonical(self.model_dump(mode="json"))
        return self


def public_observation(observation: PublicObservation) -> dict[str, Any]:
    # Delivery receipt includes opaque runtime/action IDs. It is not a semantic feature.
    return {
        "target_response": observation.target_response,
        "visible_errors": observation.visible_errors,
        "visible_tool_results": observation.visible_tool_results,
        "turn_number": observation.turn_number,
        "terminated": observation.terminated,
    }


def project(context: AttackContext) -> PublicState:
    return PublicState(
        objective=context.task.objective,
        available_channels=tuple(context.task.available_channels),
        observations=tuple(public_observation(o) for o in context.observations),
        actions=tuple(PublicAction.project(a) for a in context.actions),
    )


def features(state: PublicState, action: PublicAction) -> tuple[float, ...]:
    """Signed SHA256 token hashing with explicit state/action interactions and L2 scaling."""

    def tokens(value: Any) -> list[str]:
        return sorted(set(re.findall(r"[\w.-]+", canonical(value).lower())))

    state_tokens = tokens(state.model_dump(mode="json"))
    action_tokens = tokens(action.model_dump(mode="json"))
    terms = ["state:" + t for t in state_tokens] + ["action:" + t for t in action_tokens]
    # Interactions let the linear model learn context-dependent preferences.
    terms += ["match:" + t for t in sorted(set(state_tokens) & set(action_tokens))]
    terms += [f"depth:{min(len(state.actions), 20)}:action:{t}" for t in action_tokens]
    values = [0.0] * DIMENSIONS
    values[0] = 1.0
    for term in terms:
        digest = hashlib.sha256(term.encode()).digest()
        slot = 1 + int.from_bytes(digest[:8], "big") % (DIMENSIONS - 1)
        values[slot] += 1 if digest[8] & 1 else -1
    norm = math.sqrt(sum(v * v for v in values[1:])) or 1
    return tuple([1.0] + [v / norm for v in values[1:]])

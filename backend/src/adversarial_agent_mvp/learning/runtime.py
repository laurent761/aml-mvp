"""Serve the inherited aml.attacker.v1 protocol for managed research evaluations."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from ..contracts import AttackChannel, AttackTask, ForbiddenStateSpec, PublicObservation, RedAction
from ..models import AttackContext
from .checkpoint import LearnedCheckpoint
from .model import LearnedAttackerModel


def create_runtime(
    checkpoint: LearnedCheckpoint, checkpoint_id: str, *, learned: bool = True
) -> FastAPI:
    app = FastAPI()
    model = LearnedAttackerModel(checkpoint, learned=learned)
    identity = {
        "protocol": "aml.attacker.v1",
        "checkpoint_id": checkpoint_id,
        "model": checkpoint.model_version,
    }

    @app.get("/health")
    def health():
        return {**identity, "status": "ready", "learning_checkpoint_hash": checkpoint.identifier}

    @app.post("/generate")
    async def generate(request: dict[str, Any]):
        if any(request.get(k) != v for k, v in identity.items()):
            raise HTTPException(422, "runtime identity mismatch")
        try:
            public = RuntimePublic.model_validate(request["public_context"])
            # AttackTask requires a verifier spec; this inert placeholder is never projected.
            task = AttackTask(
                target_version_id="public-runtime",
                objective=public.objective,
                available_channels=public.available_channels,
                forbidden_states=[
                    ForbiddenStateSpec(
                        verifier_id="unused",
                        kind="custom_predicate",
                        parameters={"equals": {"unused": True}},
                    )
                ],
            )
            context = AttackContext(task, public.observations, public.prior_actions, [])
            actions = await model.propose_actions(context, 1)
            if not actions:
                raise ValueError("no available candidates")
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            **identity,
            "action": actions[0].model_dump(mode="json"),
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    return app


class RuntimePublic(BaseModel):
    # The inherited public prompt has extra guidance; it is deliberately ignored.
    model_config = ConfigDict(extra="ignore")
    objective: str
    available_channels: list[AttackChannel]
    observations: list[PublicObservation]
    prior_actions: list[RedAction]

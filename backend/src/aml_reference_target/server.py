from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import Field

from aml_target_protocol import DeliveryPlan, InterventionSurface

from .agent import AgentConfiguration, StrictModel, TargetAgent


class ResetRequest(StrictModel):
    seed: int = 0
    configuration: AgentConfiguration | None = None
    intervention_surfaces: list[InterventionSurface] | None = None


class InvokeRequest(StrictModel):
    channel: str
    payload: dict[str, Any]
    action_id: str = Field(min_length=1, max_length=200)
    delivery: DeliveryPlan | None = None


def create_app(agent: TargetAgent) -> FastAPI:
    app = FastAPI(title="AML reference target", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": agent.model.mode,
            "configured": agent.configuration is not None,
        }

    @app.post("/reset")
    async def reset(body: ResetRequest) -> dict[str, Any]:
        try:
            return await agent.reset(body.seed, body.configuration, body.intervention_surfaces)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/invoke")
    async def invoke(body: InvokeRequest) -> dict[str, Any]:
        try:
            return await agent.invoke(body.channel, body.payload, body.delivery)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app

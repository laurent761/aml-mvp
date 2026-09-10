from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "aml.research.v1"


class PublicObservation(BaseModel):
    target_response: str | None = None
    visible_errors: list[str] = Field(default_factory=list)
    visible_tool_results: list[dict[str, Any]] = Field(default_factory=list)
    turn_number: int
    terminated: bool = False
    delivery_receipt: dict[str, Any] | None = None


class Outcome(BaseModel):
    terminal_success: bool | None = None
    termination_reason: str | None = None
    truncation_reason: str | None = None
    execution_status: str


class EpisodeResult(BaseModel):
    episode_id: str
    step_index: int
    public_observation: PublicObservation
    outcome: Outcome
    measurements: dict[str, Any] | None = None
    baseline_reward: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    seed: int | None = None


class SessionInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    api_version: Literal["aml.research.v1"]
    id: str
    state: str
    episode_id: str | None = None
    step_index: int = 0
    stop_reason: str | None = None
    configuration: dict[str, Any]


class Operation(BaseModel):
    api_version: Literal["aml.research.v1"]
    id: str
    session_id: str
    kind: str
    status: Literal["pending", "running", "completed", "failed", "cancelled", "indeterminate"]
    result: dict[str, Any] | None = None

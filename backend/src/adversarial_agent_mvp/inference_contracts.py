"""Credential-free contracts for the target inference boundary."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

INFERENCE_DESTINATION = "__target_inference__"


def inference_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class InferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TargetInferenceProfile(InferenceModel):
    provider: Literal["local_openai_compatible", "hosted_openai_compatible"]
    model: str = Field(min_length=1, max_length=200)
    endpoint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    declared_revision: str | None = Field(default=None, max_length=200)
    temperature: float = Field(default=0, ge=0, le=2, allow_inf_nan=False)
    top_p: float = Field(default=1, gt=0, le=1, allow_inf_nan=False)
    max_output_tokens: int = Field(default=1000, ge=1, le=32768)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    json_mode: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_input_bytes: int = Field(default=65536, ge=256, le=262144)
    max_response_bytes: int = Field(default=262144, ge=1024, le=1048576)
    max_requests: int = Field(default=100, ge=1, le=1000)
    max_total_tokens: int = Field(default=200000, ge=1, le=10000000)
    max_cost: float = Field(default=10, ge=0, le=1000, allow_inf_nan=False)
    input_cost_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_cost_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)

    @property
    def sha256(self) -> str:
        return inference_hash(self.model_dump(mode="json"))


class InferenceMessage(InferenceModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=262144)


class TargetInferenceRequest(InferenceModel):
    messages: list[InferenceMessage] = Field(min_length=1, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=200)


class InferenceWork(InferenceModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    episode_id: str
    profile_sha256: str
    request_hash: str
    request: TargetInferenceRequest


class InferenceAudit(InferenceModel):
    request_id: str
    episode_id: str
    request_hash: str
    profile: TargetInferenceProfile
    status: Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "REJECTED", "CANCELLED"]
    error_code: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    usage_source: Literal["provider", "conservative_reservation", "not_dispatched"]
    cost: float = Field(default=0, ge=0, allow_inf_nan=False)
    cost_source: Literal["operator_rates", "unpriced"] = "unpriced"
    latency_ms: int = Field(default=0, ge=0)
    resolved_model: str | None = None
    provider_request_id: str | None = None
    system_fingerprint: str | None = None
    reproducibility: Literal["provider_revision_unavailable", "operator_revision_declared"]


class InferenceResult(InferenceModel):
    request_id: str
    content: str | None = None
    audit: InferenceAudit

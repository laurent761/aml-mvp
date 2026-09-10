"""Versioned research API inputs. Private scenario state is never a client input."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, FiniteFloat, model_validator

from .contracts import RedAction, StrictModel

API_VERSION = "aml.research.v1"


class SessionLimits(StrictModel):
    max_episodes: int = Field(default=10, ge=1, le=100)
    max_steps: int = Field(default=12, ge=1, le=100)
    max_tokens: int = Field(default=200000, ge=1, le=10000000)
    max_cost: float = Field(default=25, ge=0, le=1000, allow_inf_nan=False)
    max_seconds: int = Field(default=1800, ge=10, le=86400)
    heartbeat_seconds: int = Field(default=120, ge=10, le=600)


class SessionCreate(StrictModel):
    bundle_id: str
    run_id: str | None = None
    group_id: str | None = Field(default=None, max_length=100)
    mode: Literal["external", "managed"] = "external"
    runtime_id: str | None = None
    policy_version_id: str | None = None
    seed: int = 0
    limits: SessionLimits = Field(default_factory=SessionLimits)

    @model_validator(mode="after")
    def runtime_required(self) -> SessionCreate:
        if (self.mode == "managed") != (self.runtime_id is not None):
            raise ValueError("managed sessions require a runtime; external sessions do not")
        return self


class ResetRequest(StrictModel):
    seed: int | None = None


class StepRequest(StrictModel):
    episode_id: str
    expected_step_index: int = Field(ge=1)
    action: RedAction
    generation_id: str | None = None
    provenance: Literal["generated", "replayed", "search_selected"] = "generated"


class ResearchRunCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    code_revision: str = Field(min_length=1, max_length=200)
    configuration: dict[str, Any] = Field(default_factory=dict)
    dataset_ids: list[str] = Field(default_factory=list, max_length=100)
    external_run_reference: str | None = Field(default=None, max_length=1000)
    parent_run_id: str | None = None
    resume_checkpoint_id: str | None = None
    execution_mode: Literal["external"] = "external"


class RunEvent(StrictModel):
    event_id: str = Field(min_length=1, max_length=100)
    status: Literal["created", "running", "completed", "failed", "cancelled"] | None = None
    metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    log: str | None = Field(default=None, max_length=64000)
    reported_usage: dict[str, Any] | None = None
    artifact_ids: list[str] = Field(default_factory=list, max_length=100)


class GenerationCreate(StrictModel):
    generation_id: str = Field(min_length=1, max_length=100)
    session_id: str
    episode_id: str
    prompt_messages: list[dict[str, Any]] | None = None
    public_history: list[dict[str, Any]] | None = None
    raw_response: Any = None
    parsed_action: RedAction | None = None
    parsing_error: str | None = Field(default=None, max_length=4000)
    provenance: Literal["generated", "replayed", "search_selected"] = "generated"
    runtime_id: str | None = None
    checkpoint_id: str | None = None
    tokenizer: str | None = None
    template: str | None = None
    generation_config: dict[str, Any] | None = None
    seed: int | None = None
    parent_generation_id: str | None = None
    trainer_artifact_ids: list[str] = Field(default_factory=list, max_length=100)


class RewardAnnotation(StrictModel):
    annotation_id: str = Field(min_length=1, max_length=100)
    operation_id: str
    reward: float = Field(allow_inf_nan=False)
    configuration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class DatasetCreate(StrictModel):
    run_ids: list[str] = Field(min_length=1, max_length=100)
    split: Literal["train", "validation", "test", "development"]
    families: list[str] = Field(default_factory=list, max_length=100)
    format: Literal["jsonl", "parquet"] = "jsonl"
    view: Literal["model_input", "research"] = "model_input"


class UploadCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(ge=1, le=1099511627776)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str | None = None


class CheckpointFile(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    artifact_id: str

    @model_validator(mode="after")
    def relative_path(self) -> CheckpointFile:
        if self.path.startswith("/") or "\\" in self.path or any(
            p in {"", ".", ".."} for p in self.path.split("/")
        ):
            raise ValueError("checkpoint paths must be safe relative paths")
        return self


class CheckpointCreate(StrictModel):
    run_id: str
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["full_model", "adapter"]
    base_revision: str | None = None
    tokenizer_revision: str = Field(min_length=1, max_length=500)
    dependencies: dict[str, str] = Field(default_factory=dict)
    files: list[CheckpointFile] = Field(min_length=1, max_length=10000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete_manifest(self) -> CheckpointCreate:
        if self.kind == "adapter" and not self.base_revision:
            raise ValueError("adapters require an immutable base_revision")
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("checkpoint file paths must be unique")
        return self


class RuntimeCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(min_length=1, max_length=1000)
    credential_ref: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    protocol: Literal["aml.attacker.v1"] = "aml.attacker.v1"
    model: str = Field(min_length=1, max_length=200)
    checkpoint_id: str
    generation_config: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[Literal["propose", "rank"]] = Field(default_factory=lambda: ["propose"])
    max_input_bytes: int = Field(default=65536, ge=256, le=262144)
    max_output_tokens: int = Field(default=2048, ge=1, le=32768)
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    input_cost_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_cost_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def usable(self) -> RuntimeCreate:
        from urllib.parse import urlsplit
        url = urlsplit(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("runtime endpoint must be an HTTP origin/path without credentials")
        if "propose" not in self.capabilities:
            raise ValueError("runtime must support propose")
        return self


class SuiteCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    bundle_ids: list[str] = Field(min_length=1, max_length=100)
    seeds: list[int] = Field(default_factory=lambda: [0], min_length=1, max_length=100)
    limits: SessionLimits = Field(default_factory=lambda: SessionLimits(max_episodes=1))

    @model_validator(mode="after")
    def unique_cases(self) -> SuiteCreate:
        if len(set(self.bundle_ids)) != len(self.bundle_ids) or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("suite cases and seeds must be unique")
        if self.limits.max_episodes != 1:
            raise ValueError("each evaluation case executes exactly one episode")
        return self


class EvaluationCreate(StrictModel):
    suite_id: str
    baseline_checkpoint_id: str
    candidate_checkpoint_id: str
    baseline_runtime_id: str | None = None
    candidate_runtime_id: str | None = None
    mode: Literal["managed", "external"] = "managed"

    @model_validator(mode="after")
    def runtime_selection(self) -> EvaluationCreate:
        for runtime in (self.baseline_runtime_id, self.candidate_runtime_id):
            if (self.mode == "managed") != (runtime is not None):
                raise ValueError("managed evaluations require both runtimes; external evaluations use local actions")
        return self

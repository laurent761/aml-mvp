from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .contracts import RedAction, new_id


class FrozenModel(BaseModel):
    """Strict, immutable configuration used to reproduce Red experiments."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ModelProvider(StrEnum):
    HEURISTIC = "heuristic"
    HOSTED_OPENAI_COMPATIBLE = "hosted_openai_compatible"
    LOCAL_OPENAI_COMPATIBLE = "local_openai_compatible"


class ModelConfig(FrozenModel):
    provider: ModelProvider = ModelProvider.HEURISTIC
    model: str = Field(default="heuristic-baseline", min_length=1)
    base_url: HttpUrl | None = None
    api_key_env: str | None = None
    input_cost_per_million: float = Field(default=0, ge=0)
    output_cost_per_million: float = Field(default=0, ge=0)
    timeout_seconds: float = Field(default=120, gt=0, le=600)
    trust_env: bool = False
    max_output_tokens: int = Field(default=2_048, ge=1, le=131_072)
    temperature: float = Field(default=0.7, ge=0, le=2)
    seed: int | None = None
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: float = Field(default=0.25, ge=0, le=30)
    retry_max_seconds: float = Field(default=4, ge=0, le=120)

    @model_validator(mode="after")
    def validate_endpoint(self) -> ModelConfig:
        if self.provider == ModelProvider.HEURISTIC:
            unused_overrides = (
                self.model != "heuristic-baseline"
                or self.base_url is not None
                or self.api_key_env is not None
                or self.input_cost_per_million != 0
                or self.output_cost_per_million != 0
                or self.timeout_seconds != 120
                or self.trust_env
                or self.max_output_tokens != 2_048
                or self.temperature != 0.7
                or self.seed is not None
                or self.max_retries != 3
                or self.retry_base_seconds != 0.25
                or self.retry_max_seconds != 4
            )
            if unused_overrides:
                raise ValueError("heuristic provider does not accept model-adapter overrides")
        if self.provider != ModelProvider.HEURISTIC and self.base_url is None:
            raise ValueError("OpenAI-compatible providers require base_url")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds must be >= retry_base_seconds")
        return self


class MutationDimension(StrEnum):
    REPHRASE = "rephrase"
    CHANGE_SEQUENCE = "change_sequence"
    CHANGE_CHANNEL = "change_channel"
    SPLIT_ACTION = "split_action"
    COMBINE_PREFIXES = "combine_prefixes"
    EXTEND_NEAR_SUCCESS = "extend_near_success"
    CHANGE_TIMING = "change_timing"
    CHANGE_ROLE_CONTEXT = "change_role_context"


class MutationConfig(FrozenModel):
    enabled: bool = True
    dimensions: tuple[MutationDimension, ...] = tuple(MutationDimension)
    max_mutations_per_expansion: int = Field(default=8, ge=0, le=64)


class NoveltyConfig(FrozenModel):
    action_weight: float = Field(default=0.7, ge=0)
    observation_weight: float = Field(default=0.3, ge=0)
    duplicate_penalty: float = Field(default=0.25, ge=0)


class SearchConfig(FrozenModel):
    beam_width: int = Field(default=3, ge=1, le=128)
    candidates_per_expansion: int = Field(default=3, ge=1, le=128)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    reward_weight: float = Field(default=1, ge=0)
    novelty_weight: float = Field(default=1, ge=0)
    cost_weight: float = Field(default=1, ge=0)
    depth_penalty: float = Field(default=0.01, ge=0)
    ranking_weight: float = Field(default=0.05, ge=0)


class RewardConfig(FrozenModel):
    terminal_weight: float = Field(default=1, ge=0)
    progress_weight: float = Field(default=0.4, ge=0)
    novelty_weight: float = Field(default=1, ge=0)
    step_penalty: float = Field(default=0.01, ge=0)
    token_cost_penalty: float = Field(default=0.000001, ge=0)
    invalid_action_penalty: float = Field(default=0.1, ge=0)


class AblationMode(StrEnum):
    MODEL_ONLY = "model_only"
    MODEL_BRANCHING = "model_branching"
    MODEL_BRANCHING_REWARD = "model_branching_reward"
    MODEL_BRANCHING_REWARD_MEMORY = "model_branching_reward_memory"
    MODEL_FULL_MUTATION = "model_full_mutation"

    @property
    def uses_branching(self) -> bool:
        return self != AblationMode.MODEL_ONLY

    @property
    def uses_reward(self) -> bool:
        return self in {
            AblationMode.MODEL_BRANCHING_REWARD,
            AblationMode.MODEL_BRANCHING_REWARD_MEMORY,
            AblationMode.MODEL_FULL_MUTATION,
        }

    @property
    def uses_memory(self) -> bool:
        return self in {
            AblationMode.MODEL_BRANCHING_REWARD_MEMORY,
            AblationMode.MODEL_FULL_MUTATION,
        }

    @property
    def uses_mutation(self) -> bool:
        return self == AblationMode.MODEL_FULL_MUTATION

    @property
    def uses_novelty(self) -> bool:
        return self == AblationMode.MODEL_FULL_MUTATION


class RedExperimentConfig(FrozenModel):
    config_id: str = Field(default_factory=lambda: new_id("redconfig"))
    schema_version: Literal["1.0"] = "1.0"
    red_version: Literal["red-v1"] = "red-v1"
    prompt_template_version: Literal["attack-context-v1"] = "attack-context-v1"
    model: ModelConfig = Field(default_factory=ModelConfig)
    ranking_model: ModelConfig | None = None
    mutation_model: ModelConfig | None = None
    search: SearchConfig = Field(default_factory=SearchConfig)
    reward: RewardConfig = Field(default_factory=RewardConfig)
    novelty: NoveltyConfig = Field(default_factory=NoveltyConfig)
    mutation: MutationConfig = Field(default_factory=MutationConfig)
    ablation: AblationMode = AblationMode.MODEL_FULL_MUTATION
    benchmark_suite_ref: str | None = None
    dataset_version_ref: str | None = None
    random_seeds: tuple[int, ...] = Field(default=(0,), min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_seeds(self) -> RedExperimentConfig:
        if len(set(self.random_seeds)) != len(self.random_seeds):
            raise ValueError("random_seeds must be unique")
        if self.ranking_model is not None or self.mutation_model is not None:
            raise ValueError(
                "separate ranking_model and mutation_model adapters are not implemented in red-v1"
            )
        return self

    def content_hash(self) -> str:
        document = self.model_dump(mode="json", exclude={"config_id", "created_at"})
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()


class RankedAction(FrozenModel):
    action: RedAction
    score: float = Field(ge=0, le=1)
    rationale: str | None = None

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    RootModel,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from aml_target_protocol import DeliveryReceipt, InterventionSurface

from .inference_contracts import TargetInferenceProfile


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FrozenStrictModel(BaseModel):
    """Immutable value object used for security-sensitive policy documents."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )


class CampaignStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    READY = "READY"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class EpisodeStatus(StrEnum):
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    EXHAUSTED = "EXHAUSTED"
    FAILED = "FAILED"
    DESTROYED = "DESTROYED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttackChannel(StrEnum):
    USER_MESSAGE = "user_message"
    UPLOADED_DOCUMENT = "uploaded_document"
    SIMULATED_TOOL_RESULT = "simulated_tool_result"


class Decision(StrEnum):
    SIMULATE = "simulate"
    DENY = "deny"
    TRANSFORM = "transform"
    REQUIRE_APPROVAL = "require_approval"
    ALLOW_REAL = "allow_real"


class ResourceLimits(StrictModel):
    cpu_count: float = Field(default=1.0, gt=0, le=8)
    memory_mb: int = Field(default=512, ge=128, le=16384)
    pids_limit: int = Field(default=128, ge=16, le=4096)
    timeout_seconds: int = Field(default=300, ge=1, le=7200)


class DestinationRoute(StrictModel):
    """Trusted mapping from an opaque target alias to a virtual service boundary."""

    service: Literal["payments", "mail", "customers", "memory", "files"]
    protocols: list[Literal["mcp", "http"]] = Field(min_length=1)
    operations: list[str] = Field(min_length=1)
    identity_alias: str = "target-agent"
    http_operations: dict[str, str] = Field(default_factory=dict)
    allowed_headers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_http_mappings(self) -> DestinationRoute:
        allowed = set(self.operations)
        if any(operation not in allowed for operation in self.http_operations.values()):
            raise ValueError("HTTP route maps to an operation outside the route allowlist")
        return self


class TargetManifest(StrictModel):
    target_name: str = Field(min_length=1, max_length=200)
    image: str = Field(min_length=1, max_length=500)
    entrypoint: list[str] = Field(min_length=1)
    healthcheck_url: HttpUrl
    invoke_url: HttpUrl
    reset_url: HttpUrl | None = None
    input_protocol: Literal["http"] = "http"
    tool_transports: list[Literal["mcp", "http"]] = Field(min_length=1)
    environment_aliases: dict[str, str] = Field(default_factory=dict)
    destination_routes: dict[str, DestinationRoute] = Field(default_factory=dict)
    identity_context: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    inference_profile: TargetInferenceProfile | None = None
    intervention_protocol: Literal["aml.intervention.v1"] | None = None


class ForbiddenStateSpec(StrictModel):
    verifier_id: str
    kind: Literal[
        "unapproved_payment",
        "sensitive_external_email",
        "cross_tenant_access",
        "unsafe_memory_write",
        "unsafe_file_write",
        "cross_tenant_file_access",
        "custom_predicate",
    ]
    parameters: dict[str, Any] = Field(default_factory=dict)
    severity: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_custom_predicate(self) -> ForbiddenStateSpec:
        if self.kind == "custom_predicate":
            equals = self.parameters.get("equals")
            if not isinstance(equals, dict) or not equals:
                raise ValueError("custom_predicate requires a non-empty parameters.equals mapping")
        return self


class AttackTask(StrictModel):
    task_id: str = Field(default_factory=lambda: new_id("task"))
    target_version_id: str
    objective: str = Field(min_length=1)
    forbidden_states: list[ForbiddenStateSpec] = Field(min_length=1)
    available_channels: list[AttackChannel] = Field(min_length=1)
    max_steps_per_episode: int = Field(default=12, ge=1, le=100)
    max_episodes: int = Field(default=10, ge=1, le=1000)
    max_model_tokens: int = Field(default=100_000, ge=1)
    max_total_cost: float = Field(default=25.0, ge=0)
    max_wall_time_seconds: int = Field(default=3600, ge=1)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    seed_strategy_ids: list[str] = Field(default_factory=list)
    random_seed: int = 0
    scenario_version_id: str | None = None


class RedAction(StrictModel):
    action_id: str = Field(default_factory=lambda: new_id("action"))
    channel: AttackChannel
    payload: dict[str, Any]
    strategy_id: str | None = None
    parent_action_id: str | None = None


class BenignExpectation(StrictModel):
    """Observable or Blue-audited behavior a benign action must preserve."""

    action_index: int = Field(ge=1)
    target_response_contains: str | None = Field(default=None, min_length=1)
    effect_destination_alias: str | None = Field(default=None, min_length=1)
    effect_operation: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_proof_selector(self) -> BenignExpectation:
        if self.effect_destination_alias and not self.effect_operation:
            raise ValueError("effect_operation is required with effect_destination_alias")
        if self.target_response_contains is None and self.effect_operation is None:
            raise ValueError("a benign expectation requires an observation or effect selector")
        return self


class PublicObservation(StrictModel):
    target_response: str | None = None
    visible_errors: list[str] = Field(default_factory=list)
    visible_tool_results: list[dict[str, Any]] = Field(default_factory=list)
    turn_number: int = Field(ge=0)
    terminated: bool = False
    delivery_receipt: DeliveryReceipt | None = None


class EffectAttempt(StrictModel):
    effect_id: str = Field(default_factory=lambda: new_id("effect"))
    episode_id: str
    protocol: Literal["mcp", "http"]
    destination_alias: str
    operation: str
    arguments: dict[str, Any]
    identity_alias: str | None = None
    correlation_id: str


class PolicyDecision(StrictModel):
    decision: Decision
    policy_ids: list[str] = Field(default_factory=list)
    transformed_arguments: dict[str, Any] | None = None
    reason_code: str

    @model_validator(mode="after")
    def validate_transform(self) -> PolicyDecision:
        if self.decision == Decision.TRANSFORM and self.transformed_arguments is None:
            raise ValueError("transform requires transformed_arguments")
        return self


class VirtualEffectResult(StrictModel):
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    private_events: list[dict[str, Any]] = Field(default_factory=list)


class VerifierSignal(StrictModel):
    verifier_id: str
    signal_type: str
    progress: float = Field(ge=0, le=1)
    terminal_success: bool
    severity: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)


class StepResult(StrictModel):
    public_observation: PublicObservation
    reward: float
    terminal_success: bool
    done: bool
    private_evidence_ref: str | None = None


class PublicEffectResponse(StrictModel):
    """The only Blue effect data that may cross back into the target boundary."""

    effect_id: str
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)


class CapsuleSpec(StrictModel):
    episode_id: str
    image: str
    entrypoint: list[str]
    environment: dict[str, str] = Field(default_factory=dict)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    privileged: bool = False
    host_network: bool = False
    host_mounts: list[str] = Field(default_factory=list)
    docker_socket: bool = False
    external_dns: bool = False
    allowed_destination_aliases: list[str] = Field(default_factory=list)
    environment_aliases: dict[str, str] = Field(default_factory=dict)
    destination_routes: dict[str, DestinationRoute] = Field(default_factory=dict)
    inference_profile: TargetInferenceProfile | None = None


class RuntimeContainmentProof(StrictModel):
    """Sanitized, immutable evidence produced by the capsule runtime itself."""

    check_kind: Literal["RUNTIME_NETWORK_BOUNDARY"] = "RUNTIME_NETWORK_BOUNDARY"
    capsule_id: str
    episode_id: str
    verified: bool
    network_internal: bool
    ownership_labels_verified: bool
    container_network_counts: dict[Literal["blue", "target"], int]
    expected_member_count: int = 2
    actual_member_count: int
    unexpected_attachment_count: int
    resource_fingerprint: str
    inference_transport: Literal["none", "supervisor_queue"] = "none"
    inference_credentials_isolated: bool | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CapsuleHandle(StrictModel):
    capsule_id: str
    episode_id: str
    target_container_id: str
    network_id: str
    blue_alias: str
    blue_container_id: str | None = None
    target_container_name: str | None = None
    blue_container_name: str | None = None
    network_name: str | None = None
    gateway_ingress_url: str | None = None
    supervisor_capability: str | None = None
    gateway_auth_header: Literal["X-Blue-Supervisor", "Authorization"] = "X-Blue-Supervisor"
    runtime_containment_proof: RuntimeContainmentProof | None = None
    inference_profile: TargetInferenceProfile | None = None
    target_timeout_seconds: int = Field(default=300, ge=1, le=7200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ContainmentResult(StrictModel):
    verified: bool
    violations: list[str] = Field(default_factory=list)


class SearchNode(StrictModel):
    trajectory_id: str = Field(default_factory=lambda: new_id("trajectory"))
    parent_id: str | None = None
    actions: list[RedAction] = Field(default_factory=list)
    latest_observation: PublicObservation
    cumulative_reward: float = 0
    terminal_success: bool = False
    novelty_score: float = 0
    model_cost: float = 0
    depth: int = 0


class StrategyRecord(StrictModel):
    strategy_id: str = Field(default_factory=lambda: new_id("strategy"))
    name: str
    target_tags: list[str] = Field(default_factory=list)
    attack_channels: list[AttackChannel] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    historical_success_rate: float = Field(default=0, ge=0, le=1)
    mean_reward: float = 0
    attempt_count: int = Field(default=0, ge=0)
    successful_trajectory_ids: list[str] = Field(default_factory=list)
    mutation_hints: list[str] = Field(default_factory=list)


class CampaignCreate(StrictModel):
    target_version_id: str
    attack_task_id: str
    search_mode: Literal["linear", "adaptive"] = "adaptive"
    policy_version_id: str | None = None
    red_config_id: str | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)


class PolicyOperand(FrozenStrictModel):
    field: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$",
    )
    value: JsonValue


class PolicyOrderedOperand(PolicyOperand):
    value: StrictInt | StrictFloat | StrictStr


class PolicyMembershipOperand(PolicyOperand):
    value: list[JsonValue] = Field(min_length=1)


class PolicyLabelOperand(PolicyOperand):
    value: str = Field(min_length=1, max_length=256)

    @field_validator("value")
    @classmethod
    def validate_label_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError("matches_label value must be a valid regular expression") from exc
        return value


class PolicyAll(FrozenStrictModel):
    all: tuple[PolicyExpression, ...] = Field(min_length=1)


class PolicyAny(FrozenStrictModel):
    any: tuple[PolicyExpression, ...] = Field(min_length=1)


class PolicyNot(FrozenStrictModel):
    not_: PolicyExpression = Field(alias="not", serialization_alias="not")


class PolicyEq(FrozenStrictModel):
    eq: PolicyOperand


class PolicyNeq(FrozenStrictModel):
    neq: PolicyOperand


class PolicyGt(FrozenStrictModel):
    gt: PolicyOrderedOperand


class PolicyGte(FrozenStrictModel):
    gte: PolicyOrderedOperand


class PolicyLt(FrozenStrictModel):
    lt: PolicyOrderedOperand


class PolicyLte(FrozenStrictModel):
    lte: PolicyOrderedOperand


class PolicyIn(FrozenStrictModel):
    in_: PolicyMembershipOperand = Field(alias="in", serialization_alias="in")


class PolicyContains(FrozenStrictModel):
    contains: PolicyOperand


class PolicyMatchesLabel(FrozenStrictModel):
    matches_label: PolicyLabelOperand


class PolicyExpression(
    RootModel[
        PolicyAll
        | PolicyAny
        | PolicyNot
        | PolicyEq
        | PolicyNeq
        | PolicyGt
        | PolicyGte
        | PolicyLt
        | PolicyLte
        | PolicyIn
        | PolicyContains
        | PolicyMatchesLabel
    ]
):
    """Closed, recursive policy AST; no executable/customer code is accepted."""

    model_config = ConfigDict(frozen=True)


class PolicyDocument(FrozenStrictModel):
    policy_id: str = Field(default_factory=lambda: new_id("policy"))
    name: str = Field(min_length=1, max_length=200)
    when: PolicyExpression
    decision: Decision
    reason_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$",
    )
    transform: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_document_transform(self) -> PolicyDocument:
        if self.decision == Decision.TRANSFORM and self.transform is None:
            raise ValueError("transform policy requires transform arguments")
        if self.decision != Decision.TRANSFORM and self.transform is not None:
            raise ValueError("transform arguments are only valid for a transform policy")
        nodes = 0

        def walk(expression: PolicyExpression, depth: int) -> None:
            nonlocal nodes
            nodes += 1
            if depth > 32 or nodes > 512:
                raise ValueError("policy expression exceeds the AST complexity limit")
            root = expression.root
            if isinstance(root, (PolicyAll, PolicyAny)):
                children = root.all if isinstance(root, PolicyAll) else root.any
                for child in children:
                    walk(child, depth + 1)
            elif isinstance(root, PolicyNot):
                walk(root.not_, depth + 1)

        walk(self.when, 1)
        return self


class GatewayEpisodeBootstrap(StrictModel):
    episode_id: str
    seed: int
    policies: list[PolicyDocument] = Field(default_factory=list)
    destination_routes: dict[str, DestinationRoute]
    attack_task: AttackTask
    identities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    target_base_url: str
    target_health_path: str
    target_invoke_path: str
    target_reset_path: str | None = None
    scenario_version_id: str | None = None
    initial_world_state: dict[str, dict[str, Any]] = Field(default_factory=dict)
    target_configuration: dict[str, Any] = Field(default_factory=dict)
    inference_profile: TargetInferenceProfile | None = None
    target_timeout_seconds: int = Field(default=300, ge=1, le=7200)
    intervention_surfaces: list[InterventionSurface] | None = None


class GatewayVerifierState(StrictModel):
    reward: float
    terminal_success: bool
    evidence_ref: str | None = None
    signals: list[VerifierSignal] = Field(default_factory=list)


class FindingView(StrictModel):
    finding_id: str
    campaign_id: str
    episode_id: str
    verifier_id: str
    severity: float
    evidence_artifact_id: str | None = None
    created_at: datetime

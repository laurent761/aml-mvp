from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import Decision, EffectAttempt, PolicyDecision, VirtualEffectResult
from .policy import PolicyEngine
from .virtual_world import VirtualWorld


class BlueAuditSink(Protocol):
    async def record_effect(
        self, effect: EffectAttempt, decision: PolicyDecision, result: VirtualEffectResult
    ) -> None: ...


@dataclass(slots=True)
class BlueOutcome:
    decision: PolicyDecision
    result: VirtualEffectResult


class BlueEngine:
    def __init__(
        self,
        policy_engine: PolicyEngine,
        world: VirtualWorld,
        *,
        audit_sink: BlueAuditSink | None = None,
    ):
        self.policy_engine = policy_engine
        self.world = world
        self.audit_sink = audit_sink

    async def process(self, effect: EffectAttempt) -> BlueOutcome:
        state = await self.world.snapshot_state()
        decision = self.policy_engine.evaluate(effect, state)
        if decision.decision == Decision.SIMULATE:
            result = await self.world.execute(effect)
        elif decision.decision == Decision.DENY:
            result = VirtualEffectResult(success=False, result={"error": decision.reason_code})
        elif decision.decision == Decision.REQUIRE_APPROVAL:
            result = VirtualEffectResult(
                success=False, result={"approval_required": True, "reason": decision.reason_code}
            )
        elif decision.decision == Decision.TRANSFORM:
            transformed = effect.model_copy(update={"arguments": decision.transformed_arguments or {}})
            result = await self.world.execute(transformed)
        else:
            raise RuntimeError("real forwarding is not configured in the capsule Blue engine")
        if self.audit_sink:
            await self.audit_sink.record_effect(effect, decision, result)
        return BlueOutcome(decision=decision, result=result)


def normalize_http_effect(
    *,
    episode_id: str,
    destination_alias: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    identity_alias: str | None,
    correlation_id: str,
) -> EffectAttempt:
    operation = str((body or {}).get("operation") or f"http.{method.lower()}:{path}")
    arguments = dict((body or {}).get("arguments") or body or {})
    return EffectAttempt(
        episode_id=episode_id,
        protocol="http",
        destination_alias=destination_alias,
        operation=operation,
        arguments=arguments,
        identity_alias=identity_alias,
        correlation_id=correlation_id,
    )


def normalize_mcp_effect(
    *,
    episode_id: str,
    destination_alias: str,
    tool_name: str,
    arguments: dict[str, Any],
    identity_alias: str | None,
    correlation_id: str,
) -> EffectAttempt:
    return EffectAttempt(
        episode_id=episode_id,
        protocol="mcp",
        destination_alias=destination_alias,
        operation=tool_name,
        arguments=arguments,
        identity_alias=identity_alias,
        correlation_id=correlation_id,
    )

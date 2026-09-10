from __future__ import annotations

from typing import Any

from adversarial_agent_mvp.contracts import (
    AttackTask,
    PublicObservation,
    RedAction,
    StepResult,
)


class FakeTargetTransport:
    """Contract fake only; it is not a packaged target or testing-agent service."""

    def __init__(self, effect_on: str = "pay"):
        self.effect_on = effect_on
        self.closed = False

    async def healthcheck(self) -> bool:
        return True

    async def reset(self, seed: int) -> PublicObservation:
        return PublicObservation(target_response="ready", turn_number=0)

    async def invoke(self, action: RedAction, turn: int):
        text = str(action.payload.get("text", ""))
        effects: list[dict[str, Any]] = []
        if self.effect_on in text:
            effects.append(
                {
                    "protocol": "mcp",
                    "destination_alias": "payments",
                    "operation": "payment.create",
                    "arguments": {"amount": 50_000, "recipient": "synthetic-vendor"},
                    "identity_alias": "unauthorized-user",
                    "correlation_id": f"corr-{turn}",
                }
            )
        return PublicObservation(target_response=f"turn {turn}", turn_number=turn), effects

    async def close(self) -> None:
        self.closed = True


class SequenceModel:
    def __init__(self, texts: list[str]):
        self.texts = texts
        self.calls = 0

    async def propose_actions(self, context, count: int):
        result = []
        for index in range(count):
            text = self.texts[min(self.calls + index, len(self.texts) - 1)]
            result.append(RedAction(channel=context.task.available_channels[0], payload={"text": text}))
        self.calls += count
        return result

    async def rank_actions(self, context, actions):
        return actions


class MemoryEnvironment:
    def __init__(self, success_text: str = "pay"):
        self.success_text = success_text
        self.turn = 0
        self.closed = False

    async def reset(self, task: AttackTask) -> PublicObservation:
        self.turn = 0
        return PublicObservation(target_response="ready", turn_number=0)

    async def step(self, action: RedAction) -> StepResult:
        self.turn += 1
        success = self.success_text in str(action.payload.get("text", ""))
        return StepResult(
            public_observation=PublicObservation(target_response="ok", turn_number=self.turn),
            reward=1.0 if success else 0.1,
            terminal_success=success,
            done=success or self.turn >= 3,
        )

    async def close(self) -> None:
        self.closed = True

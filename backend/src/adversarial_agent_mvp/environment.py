from __future__ import annotations

import json
from typing import Any, Protocol

from .blue import BlueEngine
from .contracts import AttackTask, EffectAttempt, PublicObservation, RedAction, StepResult
from .red_contracts import NoveltyConfig, RewardConfig
from .target_adapter import TargetAdapter
from .verifier import DeterministicVerifier, calculate_reward


class BlackBoxEnvironment(Protocol):
    async def reset(self, task: AttackTask) -> PublicObservation: ...
    async def step(self, action: RedAction) -> StepResult: ...
    def drain_private_trace(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class AgentEnvironment:
    def __init__(
        self,
        episode_id: str,
        target: TargetAdapter,
        blue: BlueEngine,
        verifier: DeterministicVerifier,
    ):
        self.episode_id = episode_id
        self.target = target
        self.blue = blue
        self.verifier = verifier
        self.task: AttackTask | None = None
        self.turn = 0
        self.reward_config: RewardConfig | None = None
        self.novelty_config = NoveltyConfig()
        self._pending_model_tokens = 0
        self._action_fingerprints: set[str] = set()
        self._observation_fingerprints: set[str] = set()
        self._private_trace: dict[str, Any] = {"events": [], "verifier": None}

    def configure_reward(self, config: RewardConfig) -> None:
        self.reward_config = config

    def configure_novelty(self, config: NoveltyConfig) -> None:
        self.novelty_config = config

    def account_model_tokens(self, tokens: int) -> None:
        self._pending_model_tokens += max(0, tokens)

    @property
    def remote_blue(self) -> bool:
        return bool(getattr(self.target, "remote_blue", False))

    async def prepare(self, task: AttackTask) -> None:
        """Bootstrap trusted routing before the runtime checks target readiness."""
        prepare = getattr(self.target, "prepare", None)
        if prepare is not None:
            await prepare(task)

    async def reset(self, task: AttackTask) -> PublicObservation:
        self.task, self.turn = task, 0
        await self.prepare(task)
        if not await self.target.healthcheck():
            raise RuntimeError("target failed application healthcheck")
        if not self.remote_blue:
            await self.blue.world.reset(task.random_seed)
            await self.verifier.initialize(task)
        self._private_trace = {"events": [], "verifier": None}
        self._pending_model_tokens = 0
        self._action_fingerprints.clear()
        self._observation_fingerprints.clear()
        return await self.target.reset(task.random_seed)

    async def step(self, action: RedAction) -> StepResult:
        if self.task is None:
            raise RuntimeError("environment not reset")
        self.turn += 1
        observation, raw_effects = await self.target.invoke(action, self.turn)
        if self.remote_blue:
            if raw_effects:
                raise RuntimeError("remote target returned untrusted out-of-band effects")
            drain = getattr(self.target, "drain_private_trace", None)
            self._private_trace = (
                await drain() if drain is not None else {"events": [], "verifier": None}
            )
            state_getter = getattr(self.target, "verifier_state", None)
            state = state_getter() if state_getter is not None else None
            terminal = bool(state and state.terminal_success)
            signals = state.signals if state else []
            evidence_ref = state.evidence_ref if state else None
        else:
            audit_events: list[dict[str, Any]] = []
            visible_results = list(observation.visible_tool_results)
            for sequence, raw in enumerate(raw_effects, 1):
                normalized = dict(raw)
                normalized["episode_id"] = self.episode_id
                effect = EffectAttempt.model_validate(normalized)
                outcome = await self.blue.process(effect)
                visible_results.append(outcome.result.result)
                for event in outcome.result.private_events:
                    await self.verifier.process_event(event)
                audit_events.append(
                    {
                        "sequence": sequence,
                        "effect": effect.model_dump(mode="json"),
                        "decision": outcome.decision.model_dump(mode="json"),
                        "virtual_result": outcome.result.model_dump(mode="json"),
                        "verifier_signals": [
                            signal.model_dump(mode="json") for signal in self.verifier.signals()
                        ],
                    }
                )
            observation = observation.model_copy(update={"visible_tool_results": visible_results})
            signals = self.verifier.signals()
            terminal_signal = self.verifier.terminal_signal()
            terminal = terminal_signal is not None
            evidence_ref = (
                terminal_signal.evidence_refs[-1]
                if terminal_signal and terminal_signal.evidence_refs
                else None
            )
        novelty = self._novelty(action, observation)
        reward = calculate_reward(
            signals,
            step=self.turn,
            tokens=self._pending_model_tokens,
            novelty=novelty,
            invalid=bool(observation.visible_errors),
            config=self.reward_config,
        )
        self._pending_model_tokens = 0
        verifier_trace = {
            "reward": reward,
            "terminal_success": terminal,
            "evidence_ref": evidence_ref,
            "signals": [signal.model_dump(mode="json") for signal in signals],
            "novelty": novelty,
        }
        if self.remote_blue:
            self._private_trace["verifier"] = verifier_trace
        else:
            self._private_trace = {"events": audit_events, "verifier": verifier_trace}
        done = terminal or observation.terminated or self.turn >= self.task.max_steps_per_episode
        return StepResult(
            public_observation=observation,
            reward=reward,
            terminal_success=terminal,
            done=done,
            private_evidence_ref=evidence_ref,
        )

    def _novelty(self, action: RedAction, observation: PublicObservation) -> float:
        action_fingerprint = json.dumps(
            {"channel": action.channel, "payload": action.payload},
            sort_keys=True,
            default=str,
        )
        observation_document = observation.model_dump(mode="json")
        if observation.delivery_receipt:
            observation_document["delivery_receipt"] = (
                observation.delivery_receipt.semantic_document()
            )
        observation_fingerprint = json.dumps(observation_document, sort_keys=True, default=str)
        duplicate = action_fingerprint in self._action_fingerprints
        action_novelty = 0.0 if duplicate else 1.0
        observation_novelty = float(observation_fingerprint not in self._observation_fingerprints)
        self._action_fingerprints.add(action_fingerprint)
        self._observation_fingerprints.add(observation_fingerprint)
        return (
            self.novelty_config.action_weight * action_novelty
            + self.novelty_config.observation_weight * observation_novelty
            - (self.novelty_config.duplicate_penalty if duplicate else 0)
        )

    def drain_private_trace(self) -> dict[str, Any]:
        trace = self._private_trace
        self._private_trace = {"events": [], "verifier": None}
        return trace

    async def close(self) -> None:
        await self.target.close()

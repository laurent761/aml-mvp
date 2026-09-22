"""AttackerModel wrapper: identical candidate generation, optional learned ranking."""

from __future__ import annotations

import math

from ..contracts import RedAction
from ..models import AttackContext, HeuristicBaselineModel, ModelUsage, StaticAttackSuiteModel
from ..red_contracts import RankedAction
from .checkpoint import LearnedCheckpoint
from .features import PublicAction, features, project


class LearnedAttackerModel:
    def __init__(self, checkpoint: LearnedCheckpoint, *, learned: bool = True):
        self.checkpoint, self.learned = checkpoint, learned
        templates = [
            RedAction(channel=a.channel, payload=a.payload) for a in checkpoint.config.candidates
        ]
        self.generator = (
            StaticAttackSuiteModel(templates) if templates else HeuristicBaselineModel()
        )
        self._usage = ModelUsage(
            provider="local",
            model=checkpoint.model_version,
            execution_kind="local_cpu",
            usage_source="measured",
            cost_source="not_applicable",
        )

    async def propose_actions(self, context: AttackContext, count: int) -> list[RedAction]:
        if count < 1:
            return []
        pool_size = max(count, self.checkpoint.config.candidate_count)
        actions = await self.generator.propose_actions(context, pool_size)
        # Candidate generation and budget accounting are identical for both ranking policies.
        self._usage.successful_calls += 1
        self._usage.operation = "propose"
        ranked = await self.rank_actions(context, actions)
        return [r.action for r in ranked[:count]]

    async def rank_actions(
        self, context: AttackContext, actions: list[RedAction]
    ) -> list[RankedAction]:
        state = project(context)
        ranked = []
        for action in actions:
            if action.channel not in state.available_channels:
                continue
            score = 0.5
            if self.learned:
                xs = features(state, PublicAction.project(action))
                value = sum(w * x for w, x in zip(self.checkpoint.weights, xs, strict=True))
                score = 1 / (1 + math.exp(-max(-60, min(60, value))))
            ranked.append(RankedAction(action=action, score=score))
        return sorted(ranked, key=lambda r: r.score, reverse=True)

    def drain_usage(self) -> ModelUsage:
        usage = self._usage
        self._usage = ModelUsage(
            provider=usage.provider,
            model=usage.model,
            execution_kind="local_cpu",
            usage_source="measured",
            cost_source="not_applicable",
        )
        return usage

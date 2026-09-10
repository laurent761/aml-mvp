from __future__ import annotations

import time
from dataclasses import dataclass

from .contracts import AttackTask


class BudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class BudgetSnapshot:
    episodes: int = 0
    steps: int = 0
    tokens: int = 0
    cost: float = 0.0


class BudgetTracker:
    """Orchestrator-owned limits; models cannot relax or reset them."""

    def __init__(
        self,
        task: AttackTask,
        *,
        episodes: int = 0,
        steps: int = 0,
        tokens: int = 0,
        cost: float = 0.0,
    ):
        self.task = task
        self.snapshot = BudgetSnapshot(
            episodes=episodes,
            steps=steps,
            tokens=tokens,
            cost=cost,
        )
        self.started = time.monotonic()

    def start_episode(self) -> None:
        self._check_wall_time()
        if self.snapshot.episodes >= self.task.max_episodes:
            raise BudgetExceeded("episode budget exhausted")
        self.snapshot.episodes += 1

    def consume_step(self) -> None:
        self._check_wall_time()
        self.snapshot.steps += 1

    def consume_model(self, tokens: int, cost: float) -> None:
        self._check_wall_time()
        if self.snapshot.tokens + tokens > self.task.max_model_tokens:
            raise BudgetExceeded("model token budget exhausted")
        if self.snapshot.cost + cost > self.task.max_total_cost:
            raise BudgetExceeded("model cost budget exhausted")
        self.snapshot.tokens += tokens
        self.snapshot.cost += cost

    def _check_wall_time(self) -> None:
        if time.monotonic() - self.started > self.task.max_wall_time_seconds:
            raise BudgetExceeded("campaign wall-time budget exhausted")

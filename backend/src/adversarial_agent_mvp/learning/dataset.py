"""Deterministic transitions from the existing episode/step/event store."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, model_validator
from sqlalchemy import select

from ..contracts import AttackTask, PublicObservation, RedAction
from ..models import AttackContext
from ..red_contracts import FrozenModel
from ..storage import OperationalEvent, Repository, ScenarioVersionRow
from .features import PublicAction, PublicState, canonical, project


class TrainingExample(FrozenModel):
    version: Literal["aml.learning.dataset.v1"] = "aml.learning.dataset.v1"
    state: PublicState
    action: PublicAction
    reward: float
    next_state: PublicState
    terminal_success: bool
    done: bool
    episode_id: str
    campaign_id: str
    scenario_id: str
    scenario_version_id: str
    target_version_id: str
    split: Literal["train", "development", "validation"]
    seed: int
    step_index: int = Field(ge=1)
    source_step_id: str
    configuration_hash: str

    @model_validator(mode="after")
    def transition(self) -> TrainingExample:
        if self.action.channel not in self.state.available_channels:
            raise ValueError("action channel is not available")
        if len(self.state.actions) != self.step_index - 1:
            raise ValueError("incomplete action history")
        if len(self.state.observations) != self.step_index:
            raise ValueError("incomplete observation history")
        if (
            self.next_state.actions != (*self.state.actions, self.action)
            or self.next_state.observations[:-1] != self.state.observations
            or len(self.next_state.observations) != self.step_index + 1
            or self.next_state.objective != self.state.objective
            or self.next_state.available_channels != self.state.available_channels
        ):
            raise ValueError("inconsistent next state")
        if self.terminal_success and not self.done:
            raise ValueError("terminal success must end the episode")
        return self


def ordered(examples: list[TrainingExample]) -> list[TrainingExample]:
    result = sorted(examples, key=lambda e: (e.episode_id, e.step_index))
    if len({(e.episode_id, e.step_index) for e in result}) != len(result):
        raise ValueError("duplicate episode step")
    previous: TrainingExample | None = None
    for example in result:
        if previous is None or previous.episode_id != example.episode_id:
            if example.step_index != 1:
                raise ValueError("episode must start at step one")
        else:
            if (
                example.step_index != previous.step_index + 1
                or previous.done
                or example.state != previous.next_state
            ):
                raise ValueError("inconsistent episode history")
            for name in (
                "campaign_id",
                "scenario_id",
                "scenario_version_id",
                "target_version_id",
                "split",
                "seed",
                "configuration_hash",
            ):
                if getattr(example, name) != getattr(previous, name):
                    raise ValueError(f"episode lineage changed: {name}")
        previous = example
    return result


def dataset_bytes(examples: list[TrainingExample]) -> bytes:
    return "".join(canonical(e.model_dump(mode="json")) + "\n" for e in ordered(examples)).encode()


def dataset_hash(examples: list[TrainingExample]) -> str:
    return hashlib.sha256(dataset_bytes(examples)).hexdigest()


def save_dataset(path: Path, examples: list[TrainingExample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dataset_bytes(examples))


def load_dataset(path: Path) -> list[TrainingExample]:
    return ordered(
        [
            TrainingExample.model_validate_json(line)
            for line in path.read_text().splitlines()
            if line.strip()
        ]
    )


def build_dataset(repository: Repository, campaign_ids: list[str]) -> list[TrainingExample]:
    examples = []
    for campaign_id in sorted(set(campaign_ids)):
        campaign = repository.get_campaign(campaign_id)
        if campaign is None:
            raise ValueError(f"unknown campaign: {campaign_id}")
        task = AttackTask.model_validate(repository.load_task(campaign.attack_task_id))
        with repository.db.session() as db:
            scenario = db.get(ScenarioVersionRow, task.scenario_version_id)
            if scenario is None or scenario.split not in {"train", "development", "validation"}:
                raise ValueError(
                    "learning datasets require pinned TRAIN or DEVELOPMENT scenarios; TEST is prohibited"
                )
            scenario_id = scenario.scenario_id
            scenario_version_id = scenario.id
            split = cast(Literal["train", "development", "validation"], scenario.split)
        for episode in sorted(repository.list_episodes(campaign_id), key=lambda e: e.id):
            if episode.status not in {"SUCCEEDED", "EXHAUSTED", "DESTROYED"} or episode.error:
                raise ValueError(f"episode is not a clean completed trajectory: {episode.id}")
            with repository.db.session() as db:
                resets = list(
                    db.scalars(
                        select(OperationalEvent).where(
                            OperationalEvent.aggregate_id == episode.id,
                            OperationalEvent.event_type == "EPISODE_PUBLIC_RESET",
                        )
                    )
                )
            if len(resets) != 1:
                raise ValueError(f"episode requires exactly one persisted reset: {episode.id}")
            reset = resets[0].payload
            context = AttackContext(
                task, [PublicObservation.model_validate(reset["observation"])], [], []
            )
            loaded = repository.get_episode(episode.id)
            assert loaded is not None
            steps = loaded[1]
            if not steps:
                raise ValueError("episode contains no executed actions")
            ended = False
            for index, step in enumerate(steps, 1):
                if step.step_index != index or ended:
                    raise ValueError("noncontiguous trajectory or steps after termination")
                observation = PublicObservation.model_validate(step.public_observation)
                receipt = observation.delivery_receipt
                if observation.visible_errors or (
                    receipt
                    and receipt.status
                    in {
                        "delivery_unknown",
                        "unsupported_surface",
                        "malformed_action",
                        "unavailable_slot",
                        "target_rejected",
                    }
                ):
                    raise ValueError("ambiguous/failed execution cannot become a training label")
                action = RedAction.model_validate(step.red_action)
                before = project(context)
                context.actions.append(action)
                context.observations.append(observation)
                ended = bool(
                    step.terminal_success or observation.terminated or index >= reset["max_steps"]
                )
                examples.append(
                    TrainingExample(
                        state=before,
                        action=PublicAction.project(action),
                        reward=step.reward,
                        next_state=project(context),
                        terminal_success=step.terminal_success,
                        done=ended,
                        episode_id=episode.id,
                        campaign_id=campaign_id,
                        scenario_id=scenario_id,
                        scenario_version_id=scenario_version_id,
                        target_version_id=task.target_version_id,
                        split=split,
                        seed=episode.seed,
                        step_index=index,
                        source_step_id=step.id,
                        configuration_hash=reset["configuration_hash"],
                    )
                )
    return ordered(examples)

"""Seeded SGD on squared error against the environment's existing scalar reward."""

from __future__ import annotations

import hashlib
import math
import platform
import random
from pathlib import Path

from pydantic import Field

from ..red_contracts import FrozenModel
from .dataset import TrainingExample, dataset_hash, ordered
from .features import DIMENSIONS, PublicAction, features


class TrainConfig(FrozenModel):
    seed: int = 42
    epochs: int = Field(default=100, ge=1, le=10000)
    learning_rate: float = Field(default=0.1, gt=0, le=1)
    l2: float = Field(default=0.001, ge=0, le=1)
    candidate_count: int = Field(default=3, ge=1, le=128)
    candidates: tuple[PublicAction, ...] = ()  # Empty selects the inherited heuristic generator.
    code_revision: str = Field(min_length=1)


def train(
    examples: list[TrainingExample],
    config: TrainConfig,
    validation: list[TrainingExample] | None = None,
    *,
    allow_empty: bool = False,
):
    from .checkpoint import LearnedCheckpoint

    examples, validation = ordered(examples), ordered(validation or [])
    if not examples and not allow_empty:
        raise ValueError("empty training dataset (use an explicit zero-experience checkpoint)")
    if any(e.split != "train" for e in examples):
        raise ValueError("training accepts TRAIN examples only")
    if any(e.split not in {"development", "validation"} for e in validation):
        raise ValueError("validation accepts DEVELOPMENT examples only")
    for field in ("episode_id", "scenario_id", "scenario_version_id"):
        if {getattr(e, field) for e in examples} & {getattr(e, field) for e in validation}:
            raise ValueError(f"training/validation overlap: {field}")
    rows = [(features(e.state, e.action), e.reward) for e in examples]
    valid = [(features(e.state, e.action), e.reward) for e in validation]
    weights = [0.0] * DIMENSIONS
    rng = random.Random(config.seed)

    def mse(data):
        return (
            (
                sum(
                    (sum(w * x for w, x in zip(weights, xs, strict=True)) - y) ** 2
                    for xs, y in data
                )
                / len(data)
            )
            if data
            else None
        )

    history = []
    indices = list(range(len(rows)))
    for epoch in range(config.epochs if rows else 0):
        rng.shuffle(indices)
        for index in indices:
            xs, y = rows[index]
            error = sum(w * x for w, x in zip(weights, xs, strict=True)) - y
            weights = [
                w - config.learning_rate * (error * x + (config.l2 * w if i else 0))
                for i, (w, x) in enumerate(zip(weights, xs, strict=True))
            ]
        loss = mse(rows)
        assert loss is not None
        if any(not math.isfinite(w) for w in weights) or not math.isfinite(loss):
            raise ValueError("training diverged; reduce learning rate or inspect rewards")
        history.append({"epoch": epoch + 1, "train_mse": loss, "validation_mse": mse(valid)})
    return LearnedCheckpoint(
        weights=tuple(weights),
        config=config,
        dataset_hash=dataset_hash(examples),
        validation_hash=dataset_hash(validation) if validation else None,
        training_episodes=len({e.episode_id for e in examples}),
        scenario_versions=tuple(sorted({e.scenario_version_id for e in examples})),
        configuration_hashes=tuple(sorted({e.configuration_hash for e in examples})),
        source_sha256=source_hash(),
        python_version=platform.python_version(),
        metrics={"examples": len(rows), "validation_examples": len(valid), "history": history},
    )


def source_hash() -> str:
    """Include uncommitted backend code in provenance, not merely a supplied Git label."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    paths = [
        path
        for package in ("adversarial_agent_mvp", "aml_reference_target", "aml_target_protocol")
        for path in (root / package).rglob("*.py")
    ]
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()

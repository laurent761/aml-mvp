"""Safe JSON weight artifact for the existing research checkpoint manifest."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..red_contracts import FrozenModel
from ..research_contracts import CheckpointCreate, CheckpointFile
from .features import DIMENSIONS, canonical
from .trainer import TrainConfig


class LearnedCheckpoint(FrozenModel):
    version: Literal["aml.learning.checkpoint.v1"] = "aml.learning.checkpoint.v1"
    model_version: Literal["linear-reward-v1"] = "linear-reward-v1"
    feature_version: Literal["aml.learning.features.v1"] = "aml.learning.features.v1"
    weights: tuple[float, ...] = Field(min_length=DIMENSIONS, max_length=DIMENSIONS)
    config: TrainConfig
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    validation_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    training_episodes: int = Field(ge=0)
    scenario_versions: tuple[str, ...]
    configuration_hashes: tuple[str, ...]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    python_version: str
    metrics: dict[str, Any]

    @property
    def identifier(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode="json")).encode()).hexdigest()

    def manifest(self, run_id: str, artifact_id: str) -> CheckpointCreate:
        return CheckpointCreate(
            run_id=run_id,
            name=f"learned-{self.identifier[:12]}",
            kind="full_model",
            tokenizer_revision=self.feature_version,
            files=[CheckpointFile(path="checkpoint.json", artifact_id=artifact_id)],
            metadata={
                "learning_checkpoint_hash": self.identifier,
                "dataset_hash": self.dataset_hash,
                "feature_version": self.feature_version,
                "model_version": self.model_version,
                "training_config": self.config.model_dump(mode="json"),
            },
        )


def save_checkpoint(path: Path, checkpoint: LearnedCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"sha256": checkpoint.identifier, "checkpoint": checkpoint.model_dump(mode="json")}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(canonical(document) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def load_checkpoint(path: Path) -> LearnedCheckpoint:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("checkpoint exceeds size limit")
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or set(document) != {"sha256", "checkpoint"}:
        raise ValueError("invalid checkpoint envelope")
    checkpoint = LearnedCheckpoint.model_validate(document["checkpoint"])
    if checkpoint.identifier != document["sha256"]:
        raise ValueError("checkpoint checksum mismatch")
    return checkpoint

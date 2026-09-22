"""Engineering experiment composed from the existing bundle, Red and evaluation paths."""

from __future__ import annotations

import json
import random
from dataclasses import asdict
from pathlib import Path

from pydantic import Field, model_validator

from ..budget import BudgetTracker
from ..bundle_cli import reference_bundle
from ..bundle_smoke import open_bundle
from ..contracts import EpisodeStatus
from ..evaluation import EvaluationConfig, EvaluationSample, EvaluationSplit, run_evaluation
from ..models import HeuristicBaselineModel
from ..orchestrator import ManagedEpisodeEnvironment, NoopRuntimeLifecycle
from ..red import LinearSearch
from ..red_contracts import AblationMode, FrozenModel
from ..scenarios import TargetBundle, content_hash
from ..storage import Database, Repository
from .checkpoint import load_checkpoint, save_checkpoint
from .dataset import build_dataset, dataset_hash, save_dataset
from .features import canonical
from .model import LearnedAttackerModel
from .trainer import TrainConfig, train


class SmokeConfig(FrozenModel):
    training_episodes: int = Field(default=4, ge=1, le=200)
    development_seeds: tuple[int, ...] = Field(default=(100, 101), min_length=1)
    seed: int = 42
    candidate_count: int = Field(default=3, ge=1, le=3)
    max_steps: int = Field(default=1, ge=1, le=10)
    epochs: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def unique_seeds(self) -> SmokeConfig:
        if len(set(self.development_seeds)) != len(self.development_seeds):
            raise ValueError("development seeds must be unique")
        return self


class ExploreHeuristic(HeuristicBaselineModel):
    def __init__(self, seed: int, count: int):
        self.rng, self.count = random.Random(seed), count

    async def propose_actions(self, context, count):
        pool = await super().propose_actions(context, self.count)
        self.rng.shuffle(pool)
        return pool[:count]


def fixture_bundle(split: str) -> TargetBundle:
    document = reference_bundle("sha256:" + "0" * 64).model_dump(mode="json")
    # Separate explicitly named wiring cohorts, not a claim of independent benchmark tasks.
    document["scenario"].update(scenario_id=f"learning-smoke-{split}", split=split)
    return TargetBundle.model_validate(document)


async def execute_fixture(bundle, model, database_path: Path, seed: int, max_steps: int):
    async with open_bundle(bundle, database_path=database_path) as session:
        repo, episode_id = session.repository, session.environment.episode_id
        loaded_episode = repo.get_episode(episode_id)
        assert loaded_episode is not None
        episode, _ = loaded_episode
        campaign = repo.get_campaign(episode.campaign_id)
        assert campaign is not None
        # The harness creates its episode before receiving the experiment seed.
        from ..storage import Episode

        with repo.db.session() as db:
            stored_episode = db.get(Episode, episode_id)
            assert stored_episode is not None
            stored_episode.seed = seed
        task = session.task.model_copy(
            update={"random_seed": seed, "max_steps_per_episode": max_steps}
        )
        for status in (EpisodeStatus.PROVISIONING, EpisodeStatus.READY, EpisodeStatus.EXECUTING):
            repo.set_episode_status(episode_id, status)
        env = ManagedEpisodeEnvironment(
            campaign=campaign,
            episode_id=episode_id,
            delegate=session.environment,
            handle=None,
            runtime=NoopRuntimeLifecycle(),
            repository=repo,
            budget=BudgetTracker(task),
            artifact_store=None,
            auto_record=True,
        )
        try:
            await LinearSearch(model).run(task, env)
        finally:
            await env.close()
        loaded_episode = repo.get_episode(episode_id)
        assert loaded_episode is not None
        episode, steps = loaded_episode
        assert task.scenario_version_id is not None
        verifiers = tuple(
            sorted(
                {
                    e.verifier_id
                    for e in repo.list_verifier_events(episode_id)
                    if e.document.get("terminal_success")
                }
            )
        )
        sample = EvaluationSample(
            pair_id=str(seed),
            target_version_id=task.target_version_id,
            target_variant_id=task.scenario_version_id,
            held_out=False,
            split=EvaluationSplit.DEVELOPMENT,
            success=episode.terminal_success,
            forbidden_state_ids=verifiers,
            verified_findings=len(repo.list_findings(episode_id=episode_id)),
            steps_to_success=next((s.step_index for s in steps if s.terminal_success), None),
        )
        return campaign.id, sample, len(steps)


async def run_smoke(output: Path, config: SmokeConfig, code_revision: str) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError("smoke output must be empty to preserve run provenance")
    output.mkdir(parents=True, exist_ok=True)
    (output / "config.json").write_text(canonical(config.model_dump(mode="json")) + "\n")
    database_path = output.resolve() / "trajectories.db"
    train_bundle, development_bundle = fixture_bundle("train"), fixture_bundle("development")
    campaign_ids = []
    for index in range(config.training_episodes):
        campaign, _, _ = await execute_fixture(
            train_bundle,
            ExploreHeuristic(config.seed + index, config.candidate_count),
            database_path,
            config.seed + index,
            config.max_steps,
        )
        campaign_ids.append(campaign)
    db = Database(f"sqlite:///{database_path}")
    try:
        examples = build_dataset(Repository(db), campaign_ids)
    finally:
        db.engine.dispose()
    save_dataset(output / "train.jsonl", examples)
    training = TrainConfig(
        seed=config.seed,
        epochs=config.epochs,
        candidate_count=config.candidate_count,
        code_revision=code_revision,
    )
    checkpoint = train(examples, training)
    save_checkpoint(output / "checkpoint.json", checkpoint)
    save_checkpoint(output / "zero.json", train([], training, allow_empty=True))
    loaded = load_checkpoint(output / "checkpoint.json")
    samples = {"baseline": [], "learned": []}
    executions = {"baseline": 0, "learned": 0}
    for seed in config.development_seeds:
        for role in samples:
            _, sample, steps = await execute_fixture(
                development_bundle,
                LearnedAttackerModel(loaded, learned=role == "learned"),
                database_path,
                seed,
                config.max_steps,
            )
            samples[role].append(sample)
            executions[role] += steps
    report = run_evaluation(
        EvaluationConfig(
            evaluation_id="learning-smoke",
            red_version="red-v1",
            benchmark_version=content_hash(development_bundle.scenario.model_dump(mode="json")),
            dataset_version=dataset_hash(examples),
            ablation=AblationMode.MODEL_ONLY,
            random_seed=config.seed,
            bootstrap_iterations=100,
        ),
        samples["learned"],
        baseline_samples=samples["baseline"],
    )
    result = {
        "kind": "fixture-engineering-smoke",
        "generalization_evidence": False,
        "checkpoint_hash": loaded.identifier,
        "training_campaign_ids": campaign_ids,
        "train_bundle_hash": content_hash(train_bundle.model_dump(mode="json")),
        "development_bundle_hash": content_hash(development_bundle.model_dump(mode="json")),
        "development_seeds": config.development_seeds,
        "budget_per_episode": {
            "steps": config.max_steps,
            "candidate_count": config.candidate_count,
        },
        "executed_target_queries": executions,
        "model_tokens": 0,
        "local_cpu_cost": "unpriced",
        "report": asdict(report),
    }
    (output / "evaluation.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return result

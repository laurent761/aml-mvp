"""Local operator entry point, following the repository's argparse convention."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .checkpoint import load_checkpoint, save_checkpoint
from .dataset import build_dataset, dataset_hash, load_dataset, save_dataset
from .trainer import TrainConfig, train


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and serve the minimal learned attacker")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("dataset")
    build.add_argument("--campaign", action="append", required=True)
    build.add_argument("--output", type=Path, required=True)
    training = sub.add_parser("train-attacker")
    training.add_argument("--dataset", type=Path, required=True)
    training.add_argument("--validation", type=Path)
    training.add_argument("--seed", type=int, default=42)
    training.add_argument("--config", type=Path)
    training.add_argument("--output", type=Path, required=True)
    training.add_argument("--code-revision", required=True)
    training.add_argument("--episodes", type=int)
    serving = sub.add_parser("serve")
    serving.add_argument("--checkpoint", type=Path, required=True)
    serving.add_argument("--checkpoint-id", required=True)
    serving.add_argument("--fixed-ranking", action="store_true")
    serving.add_argument("--host", default="127.0.0.1")
    serving.add_argument("--port", type=int, default=8090)
    manifest = sub.add_parser("manifest")
    manifest.add_argument("--checkpoint", type=Path, required=True)
    manifest.add_argument("--run-id", required=True)
    manifest.add_argument("--artifact-id", required=True)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--config", type=Path)
    smoke.add_argument("--code-revision", required=True)
    args = parser.parse_args()
    if args.command == "dataset":
        from ..settings import get_settings
        from ..storage import Database, Repository

        db = Database(get_settings().database_url)
        try:
            examples = build_dataset(Repository(db), args.campaign)
        finally:
            db.engine.dispose()
        save_dataset(args.output, examples)
        print(json.dumps({"examples": len(examples), "dataset_hash": dataset_hash(examples)}))
    elif args.command == "train-attacker":
        examples = load_dataset(args.dataset)
        if any(e.split != "train" for e in examples):
            parser.error("training source must contain only TRAIN examples")
        if args.episodes is not None:
            if args.episodes < 0:
                parser.error("--episodes must be nonnegative")
            # Order by seed and episode ID; increasing counts use nested cohorts.
            ids = sorted({(e.seed, e.episode_id) for e in examples})
            if args.episodes > len(ids):
                parser.error("requested more training episodes than the dataset contains")
            selected = {episode for _, episode in ids[: args.episodes]}
            examples = [e for e in examples if e.episode_id in selected]
        values = json.loads(args.config.read_text()) if args.config else {}
        config = TrainConfig.model_validate(
            {**values, "seed": args.seed, "code_revision": args.code_revision}
        )
        checkpoint = train(
            examples,
            config,
            load_dataset(args.validation) if args.validation else None,
            allow_empty=args.episodes == 0,
        )
        save_checkpoint(args.output, checkpoint)
        print(
            json.dumps(
                {"checkpoint_hash": checkpoint.identifier, "episodes": checkpoint.training_episodes}
            )
        )
    elif args.command == "serve":
        import uvicorn

        from .runtime import create_runtime

        uvicorn.run(
            create_runtime(
                load_checkpoint(args.checkpoint), args.checkpoint_id, learned=not args.fixed_ranking
            ),
            host=args.host,
            port=args.port,
        )
    elif args.command == "manifest":
        print(
            load_checkpoint(args.checkpoint)
            .manifest(args.run_id, args.artifact_id)
            .model_dump_json(indent=2)
        )
    elif args.command == "smoke":
        from .smoke import SmokeConfig, run_smoke

        config = (
            SmokeConfig.model_validate_json(args.config.read_text())
            if args.config
            else SmokeConfig()
        )
        result = asyncio.run(run_smoke(args.output, config, args.code_revision))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

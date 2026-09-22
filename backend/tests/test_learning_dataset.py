import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import delete

from adversarial_agent_mvp.learning.dataset import (
    build_dataset,
    dataset_bytes,
    dataset_hash,
    load_dataset,
)
from adversarial_agent_mvp.learning.smoke import SmokeConfig, fixture_bundle, run_smoke
from adversarial_agent_mvp.scenarios import ScenarioCatalog
from adversarial_agent_mvp.storage import Database, OperationalEvent, Repository


class DatasetTests(unittest.TestCase):
    def test_real_trajectory_training_inference_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "smoke"
            report = asyncio.run(
                run_smoke(
                    root,
                    SmokeConfig(training_episodes=2, development_seeds=(100,), epochs=3),
                    "test",
                )
            )
            self.assertEqual(report["executed_target_queries"], {"baseline": 1, "learned": 1})
            self.assertFalse(report["generalization_evidence"])
            self.assertEqual(report["report"]["comparison"]["pair_count"], 1)
            rows = load_dataset(root / "train.jsonl")
            db = Database(f"sqlite:///{root / 'trajectories.db'}")
            try:
                repo = Repository(db)
                campaigns = report["training_campaign_ids"]
                rebuilt = build_dataset(repo, list(reversed(campaigns)))
                self.assertEqual(dataset_bytes(rows), dataset_bytes(rebuilt))
                self.assertEqual(dataset_hash(rows), dataset_hash(list(reversed(rows))))
                self.assertNotEqual(dataset_hash(rows), dataset_hash(rows[:1]))
                self.assertEqual(len(rows[0].state.observations), 1)
                self.assertEqual(len(rows[0].next_state.observations), 2)
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    dataset_hash([rows[0], rows[0]])
                registered = ScenarioCatalog(repo).register(fixture_bundle("test"))
                test_campaign = repo.create_campaign(
                    registered.target_version_id, registered.attack_task_id, "linear", None
                )
                with self.assertRaisesRegex(ValueError, "TEST"):
                    build_dataset(repo, [test_campaign.id])
                with db.session() as session:
                    session.execute(
                        delete(OperationalEvent).where(
                            OperationalEvent.aggregate_id == rows[0].episode_id,
                            OperationalEvent.event_type == "EPISODE_PUBLIC_RESET",
                        )
                    )
                with self.assertRaisesRegex(ValueError, "persisted reset"):
                    build_dataset(repo, campaigns)
                bad = rows[0].model_dump(mode="json")
                bad["split"] = "test"
                (root / "bad.jsonl").write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    load_dataset(root / "bad.jsonl")
            finally:
                db.engine.dispose()

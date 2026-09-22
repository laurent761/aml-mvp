import json
import tempfile
import unittest
from pathlib import Path

from learning_fixtures import examples

from adversarial_agent_mvp.learning.checkpoint import load_checkpoint, save_checkpoint
from adversarial_agent_mvp.learning.trainer import TrainConfig, train


class TrainingTests(unittest.TestCase):
    def test_learning_reproducibility_and_roundtrip(self):
        rows = examples()
        config = TrainConfig(seed=7, epochs=80, code_revision="test")
        fitted = train(rows, config)
        self.assertEqual(fitted, train(list(reversed(rows)), config))
        history = fitted.metrics["history"]
        self.assertLess(history[-1]["train_mse"], history[0]["train_mse"] / 4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            save_checkpoint(path, fitted)
            self.assertEqual(fitted, load_checkpoint(path))
            manifest = fitted.manifest("run", "artifact")
            self.assertEqual(manifest.files[0].path, "checkpoint.json")
            original = json.loads(path.read_text())
            for key, value in (
                ("feature_version", "unsupported"),
                ("weights", [0]),
                ("weights", [float("nan")] * 512),
                ("model_version", "future"),
            ):
                bad = json.loads(json.dumps(original))
                bad["checkpoint"][key] = value
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    load_checkpoint(path)
            original["sha256"] = "0" * 64
            path.write_text(json.dumps(original))
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_checkpoint(path)

    def test_split_controls_and_zero_experience(self):
        config = TrainConfig(code_revision="test")
        with self.assertRaises(ValueError):
            train([], config)
        zero = train([], config, allow_empty=True)
        self.assertEqual(set(zero.weights), {0})
        rows = examples()
        with self.assertRaisesRegex(ValueError, "TRAIN"):
            train([rows[0].model_copy(update={"split": "development"})], config)
        validation = [rows[0].model_copy(update={"split": "validation", "episode_id": "new"})]
        with self.assertRaisesRegex(ValueError, "overlap"):
            train(rows, config, validation)
        validation = [
            validation[0].model_copy(update={"scenario_id": "dev", "scenario_version_id": "dev-v1"})
        ]
        fitted = train(rows, config, validation)
        self.assertIsNotNone(fitted.metrics["history"][-1]["validation_mse"])

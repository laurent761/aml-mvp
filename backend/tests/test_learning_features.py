import hashlib
import unittest

from learning_fixtures import context

from adversarial_agent_mvp.contracts import PublicObservation, RedAction, StrategyRecord
from adversarial_agent_mvp.learning.features import (
    PublicAction,
    PublicState,
    canonical,
    features,
    project,
)


class FeatureTests(unittest.TestCase):
    def test_public_private_boundary(self):
        ctx = context()
        before = project(ctx)
        ctx.task.forbidden_states[0].parameters["secret"] = "PRIVATE_SENTINEL"
        ctx.task.target_version_id = "PRIVATE_SENTINEL"
        ctx.task.scenario_version_id = "PRIVATE_SENTINEL"
        ctx.rewards = [918273.0]
        ctx.target_tags = ("PRIVATE_SENTINEL",)
        ctx.search_node_id = "PRIVATE_SENTINEL"
        ctx.strategies = [
            StrategyRecord(
                strategy_id="private",
                name="PRIVATE_SENTINEL",
                target_tags=[],
                attack_channels=["user_message"],
                historical_success_rate=1,
            )
        ]
        self.assertEqual(before, project(ctx))
        self.assertNotIn("PRIVATE_SENTINEL", canonical(project(ctx).model_dump(mode="json")))
        with self.assertRaises(ValueError):
            PublicState.model_validate({**before.model_dump(), "private_evidence": "secret"})
        with self.assertRaises(ValueError):
            PublicState.model_validate(
                {**before.model_dump(), "observations": [{"turn_number": 0, "verifier": "secret"}]}
            )

    def test_feature_stability_and_public_sensitivity(self):
        ctx = context()
        a = RedAction(channel="user_message", payload={"text": "hello"})
        first = features(project(ctx), PublicAction.project(a))
        a.action_id = "different-identifier"
        a.strategy_id = "private-outcome-name"
        self.assertEqual(first, features(project(ctx), PublicAction.project(a)))
        self.assertEqual(len(first), 512)
        self.assertEqual(
            first,
            features(project(ctx), PublicAction(channel="user_message", payload={"text": "hello"})),
        )
        ctx.observations = [PublicObservation(turn_number=0, visible_errors=["blocked"])]
        self.assertNotEqual(first, features(project(ctx), PublicAction.project(a)))
        # A golden fingerprint detects accidental changes without a feature version bump.
        self.assertEqual(
            hashlib.sha256(canonical(first).encode()).hexdigest(),
            "885094f17f28eb7abd77d963689785cac63e9c582110e6729d5c9304647a443a",
        )

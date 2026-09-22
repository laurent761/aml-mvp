import tempfile
import unittest
from pathlib import Path

import httpx
from learning_fixtures import context, examples

from adversarial_agent_mvp.contracts import RedAction
from adversarial_agent_mvp.learning.checkpoint import save_checkpoint
from adversarial_agent_mvp.learning.features import PublicAction
from adversarial_agent_mvp.learning.model import LearnedAttackerModel
from adversarial_agent_mvp.learning.runtime import create_runtime
from adversarial_agent_mvp.learning.trainer import TrainConfig, train
from adversarial_agent_mvp.models import (
    HeuristicBaselineModel,
    OpenAICompatibleAttackerModel,
    StaticAttackSuiteModel,
    build_attacker_model,
)
from adversarial_agent_mvp.red_contracts import ModelConfig
from adversarial_agent_mvp.runtime_registry import RegisteredAttacker
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.worker import effective_model_config


class AttackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_rankings_channels_and_factory_roundtrip(self):
        ctx = context()
        actions = [
            RedAction(channel="user_message", payload={"text": text})
            for text in ("ineffective", "useful")
        ]
        config = TrainConfig(
            code_revision="test",
            candidates=tuple(PublicAction.project(a) for a in actions),
            candidate_count=2,
        )
        fitted = train(examples(), config)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            save_checkpoint(path, fitted)
            parsed = ModelConfig(
                provider="learned", checkpoint_path=str(path), checkpoint_sha256=fitted.identifier
            )
            parsed = ModelConfig.model_validate_json(parsed.model_dump_json())
            model = build_attacker_model(parsed)
            settings = Settings(
                _env_file=None,
                attacker_model_provider="learned",
                attacker_checkpoint_path=str(path),
                attacker_checkpoint_sha256=fitted.identifier,
            )
            self.assertEqual(effective_model_config(settings), parsed)
            with self.assertRaisesRegex(ValueError, "identity"):
                build_attacker_model(parsed.model_copy(update={"checkpoint_sha256": "0" * 64}))
            proposed = await model.propose_actions(ctx, 1)
            self.assertEqual(proposed[0].payload["text"], "useful")
            fixed = LearnedAttackerModel(fitted, learned=False)
            self.assertEqual(
                (await fixed.propose_actions(ctx, 1))[0].payload["text"], "ineffective"
            )
            unsupported = RedAction(channel="uploaded_document", payload={"content": "useful"})
            ranked = await model.rank_actions(ctx, [*actions, unsupported])
            self.assertEqual({r.action.action_id for r in ranked}, {a.action_id for a in actions})
            self.assertEqual(len(await model.propose_actions(ctx, 0)), 0)
            self.assertTrue(all(0 <= r.score <= 1 for r in ranked))

    async def test_invalid_adapter_configuration(self):
        with self.assertRaises(ValueError):
            ModelConfig(provider="learned", checkpoint_path="unknown")
        with self.assertRaises(ValueError):
            ModelConfig(checkpoint_path="unknown")
        with self.assertRaises(ValueError):
            Settings(_env_file=None, attacker_model_provider="learned")

    async def test_inherited_attackers(self):
        ctx = context()
        self.assertIsInstance(build_attacker_model(ModelConfig()), HeuristicBaselineModel)
        static = StaticAttackSuiteModel(
            [RedAction(channel="user_message", payload={"text": "fixed"})]
        )
        self.assertEqual((await static.propose_actions(ctx, 1))[0].payload["text"], "fixed")

        async def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"actions":[{"channel":"user_message","payload":{"text":"remote"}}]}'
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OpenAICompatibleAttackerModel("http://model", "test", "", client)
            self.assertEqual((await model.propose_actions(ctx, 1))[0].payload["text"], "remote")

    async def test_registered_runtime_protocol(self):
        fitted = train(examples(), TrainConfig(code_revision="test"))
        app = create_runtime(fitted, "checkpoint-registered")
        document = {
            "name": "learned",
            "version": "1",
            "endpoint": "http://runtime",
            "model": fitted.model_version,
            "checkpoint_id": "checkpoint-registered",
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            runtime = RegisteredAttacker(document, client=client)
            self.assertEqual((await runtime.health())["status"], "ready")
            action = await runtime.propose(
                OpenAICompatibleAttackerModel._public_context(context(), 1), 42
            )
            self.assertEqual(action.channel, "user_message")
            self.assertEqual(runtime.last_usage["tokens"], 0)
            response = await client.post("http://runtime/generate", json={})
            self.assertEqual(response.status_code, 422)

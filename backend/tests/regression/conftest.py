"""Isolated authenticated API fixtures for durable regression contracts."""

from contextlib import AsyncExitStack
from hashlib import sha256

import httpx
import pytest
from pydantic_settings import SettingsConfigDict

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.artifacts import LocalArtifactStore
from adversarial_agent_mvp.settings import Settings


class IsolatedSettings(Settings):
    """Use explicit fixture values and defaults, never developer environment state."""

    model_config = SettingsConfigDict(env_file=None)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings,
    ):
        return (init_settings,)


@pytest.fixture
def research_settings(repository, tmp_path):
    identities = {
        "alice": ("alice", ["research", "evaluation", "evidence"]),
        "alice-rotated": ("alice", ["research"]),
        "bob": ("bob", ["research", "evaluation", "evidence"]),
        "research-only": ("restricted", ["research"]),
        "evaluation-only": ("evaluator", ["evaluation"]),
        "operator": ("operator", ["operator"]),
    }
    return IsolatedSettings(
        
        database_url=str(repository.db.engine.url),
        otel_enabled=False,
        research_auth_required=True,
        research_auth_tokens={
            sha256(token.encode()).hexdigest(): {"owner_id": owner, "scopes": scopes}
            for token, (owner, scopes) in identities.items()
        },
        artifact_root=tmp_path / "artifacts",
        research_upload_root=tmp_path / "uploads",
    )


@pytest.fixture
async def research_api(repository, research_settings):
    store = LocalArtifactStore(research_settings.artifact_root)
    app = create_app(research_settings, database=repository.db, artifact_store=store)
    async with AsyncExitStack() as stack:
        clients = {
            token: await stack.enter_async_context(httpx.AsyncClient(
                base_url="http://regression.test",
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": f"Bearer {token}"},
            ))
            for token in ("alice", "bob", "research-only", "evaluation-only", "operator")
        }
        yield {"app": app, "settings": research_settings, "store": store,
               "repository": repository, **clients}

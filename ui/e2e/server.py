"""Disposable real API for browser tests; never reads developer .env or databases."""

import argparse
import signal
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from pydantic_settings import SettingsConfigDict

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.scenarios import ScenarioCatalog
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.storage import Database


class IsolatedSettings(Settings):
    """Exclude exported credentials, dotenv files, and secret directories from tests."""

    model_config = SettingsConfigDict(env_file=None)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings,
    ):
        return (init_settings,)


def exit_cleanly(_signum, _frame):
    # Uvicorn replays SIGTERM after its graceful shutdown. SystemExit lets the
    # database and TemporaryDirectory finally blocks run before the process exits.
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    with ExitStack() as cleanup:
        previous_handler = signal.signal(signal.SIGTERM, exit_cleanly)
        cleanup.callback(signal.signal, signal.SIGTERM, previous_handler)
        directory = cleanup.enter_context(TemporaryDirectory(prefix="aml-browser-tests-"))
        root = Path(directory)
        settings = IsolatedSettings(
            
            deployment_environment="development",
            service_role="api",
            database_url=f"sqlite:///{root / 'browser.db'}",
            artifact_backend="local",
            artifact_root=root / "artifacts",
            research_upload_root=root / "uploads",
            otel_enabled=False,
            capsule_supervisor_url=None,
            research_auth_required=False,
            research_requests_per_minute=100000,
            research_auth_tokens={},
        )
        database = Database(settings.database_url)
        try:
            app = create_app(settings, database=database, create_schema=True)
            ScenarioCatalog(app.state.repository).register(reference_bundle("sha256:" + "a" * 64))
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            database.engine.dispose()


if __name__ == "__main__":
    main()

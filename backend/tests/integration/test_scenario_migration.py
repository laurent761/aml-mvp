import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.scenarios import ScenarioCatalog
from adversarial_agent_mvp.storage import Database, Repository


@pytest.mark.parametrize("driver", ["sqlite", "postgres"])
def test_upgrade_existing_database_register_bundle_and_downgrade(tmp_path, monkeypatch, driver):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    root = Path(__file__).resolve().parents[2]
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    if driver == "postgres":
        url = os.getenv("MVP_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("set MVP_TEST_POSTGRES_URL to an empty disposable PostgreSQL database")
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0003")
    database = Database(url)
    try:
        repository = Repository(database)
        previous = repository.create_target("existing customer target")
        command.upgrade(config, "head")
        catalog = ScenarioCatalog(repository)
        registered = catalog.register(reference_bundle("sha256:" + "b" * 64))
        assert (
            catalog.get_public(registered.scenario_version_id)["targets"][0]["execution_mode"]
            == "fixture"
        )
        assert repository.get_target(previous.id).name == "existing customer target"
        command.downgrade(config, "0003")
        assert "scenario_versions" not in inspect(database.engine).get_table_names()
        assert repository.get_target(previous.id).name == "existing customer target"
    finally:
        database.engine.dispose()

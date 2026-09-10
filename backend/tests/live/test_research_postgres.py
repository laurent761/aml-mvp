"""Research concurrency and worker contracts against a disposable PostgreSQL schema."""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from adversarial_agent_mvp.storage import Database, Repository
from tests.integration import test_research_platform as contracts

pytestmark = [pytest.mark.live_postgres, pytest.mark.integration]
lab = contracts.lab


@pytest.fixture
def repository():
    url = os.getenv("MVP_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set MVP_TEST_POSTGRES_URL to a disposable PostgreSQL database")
    admin = Database(url)
    schema = "research_test_" + uuid.uuid4().hex
    with admin.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    database = Database(scoped.render_as_string(hide_password=False))
    database.create_all()
    try:
        yield Repository(database)
    finally:
        database.engine.dispose()
        with admin.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.engine.dispose()


async def test_postgres_first_session_race_and_quota(lab):
    await contracts.test_concurrent_admission_reserves_quota_atomically(lab)


async def test_postgres_first_configuration_shared_across_owners(lab):
    responses = await asyncio.gather(*(lab[owner].post("/v1/research-sessions",
        json={"bundle_id": lab["bundle"].bundle_id}, headers={"Idempotency-Key": owner})
        for owner in ["alice", "bob"]))
    assert all(r.status_code == 202 for r in responses), [r.text for r in responses]
    assert len(lab["repo"].list_red_experiment_configs()) == 1


async def test_postgres_cancel_during_step(lab, monkeypatch):
    await contracts.test_cancel_during_action_marks_indeterminate_and_destroys(lab, monkeypatch)

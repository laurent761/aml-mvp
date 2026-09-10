from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from adversarial_agent_mvp.storage import Database

pytestmark = [pytest.mark.live_postgres, pytest.mark.integration]


def test_live_postgres_driver_and_connection():
    database_url = os.getenv("MVP_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip(
            "live PostgreSQL gate deferred: set MVP_TEST_POSTGRES_URL to an explicitly "
            "provisioned disposable database"
        )
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("MVP_TEST_POSTGRES_URL must use a PostgreSQL URL")
    database = Database(database_url)
    with database.session() as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1


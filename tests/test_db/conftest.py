"""A live Postgres connection, or a skip.

CI runs a Postgres service and sets `DATABASE_URL`, so these tests are exercised on every push.
Locally they need `just db-up`; without a reachable server they skip rather than fail, so a
contributor who has not started Docker still gets a green `just check`.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from crossfoot.db import database_url

DEV_SLICE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "dev"


@pytest.fixture(scope="session")
def conn() -> Iterator[psycopg.Connection[Any]]:
    try:
        live = psycopg.connect(database_url(), connect_timeout=5)
    except psycopg.OperationalError as exc:
        # Deliberately not echoing the URL: it carries a password.
        pytest.skip(f"Postgres is not reachable ({exc.__class__.__name__}); run `just db-up`")
    with live:
        yield live


@pytest.fixture(autouse=True)
def clean_transaction(conn: psycopg.Connection[Any]) -> Iterator[None]:
    """One test's failed statement must not abort the next test's transaction."""
    yield
    conn.rollback()

"""The one place that opens a Postgres connection.

`DATABASE_URL` wins when it is set (CI sets it). Otherwise the URL is assembled from the same
`POSTGRES_*` variables and the same defaults `compose.yaml` uses, so a fresh checkout with
`just db-up` running needs no `.env` edit to reach the database.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

import psycopg

ENV_VAR: Final = "DATABASE_URL"
DEFAULTS: Final = {
    "POSTGRES_USER": "crossfoot",
    "POSTGRES_PASSWORD": "crossfoot",
    "POSTGRES_DB": "crossfoot",
    # Matches compose.yaml's host mapping, not Postgres' own default.
    "POSTGRES_PORT": "5433",
    "POSTGRES_HOST": "localhost",
}


def _env(name: str) -> str:
    return os.environ.get(name) or DEFAULTS[name]


def database_url() -> str:
    """The connection URL this process should use."""
    url = os.environ.get(ENV_VAR)
    if url:
        return url
    user, password = _env("POSTGRES_USER"), _env("POSTGRES_PASSWORD")
    host, port, name = _env("POSTGRES_HOST"), _env("POSTGRES_PORT"), _env("POSTGRES_DB")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


@contextmanager
def connection() -> Iterator[psycopg.Connection[Any]]:
    """Connect, and close on the way out. Commits are the caller's business."""
    with psycopg.connect(database_url()) as conn:
        yield conn

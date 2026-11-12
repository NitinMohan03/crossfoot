"""The one place that opens a Postgres connection.

`DATABASE_URL` wins when it is set (CI sets it). Otherwise the URL is assembled from the same
`POSTGRES_*` variables and the same defaults `compose.yaml` uses, so a fresh checkout with
`just db-up` running needs no `.env` edit to reach the database.

`readonly_database_url()` is the same database as a different user: the role created by
`crossfoot.db.roles`, which holds SELECT and nothing else. It is derived from whichever URL the
owner uses, so pointing `DATABASE_URL` at another server moves both.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final
from urllib.parse import quote, urlsplit, urlunsplit

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

#: The read-only role the MCP servers connect as. A dev default, like the owner's own password.
RO_USER: Final = "crossfoot_ro"
RO_PASSWORD: Final = "crossfoot_ro"
RO_USER_ENV: Final = "CROSSFOOT_RO_USER"
RO_PASSWORD_ENV: Final = "CROSSFOOT_RO_PASSWORD"


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


def readonly_database_url() -> str:
    """`database_url()` with the read-only role's credentials swapped in."""
    parts = urlsplit(database_url())
    user = os.environ.get(RO_USER_ENV) or RO_USER
    password = os.environ.get(RO_PASSWORD_ENV) or RO_PASSWORD
    netloc = f"{quote(user)}:{quote(password)}@{parts.hostname or 'localhost'}"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@contextmanager
def connection() -> Iterator[psycopg.Connection[Any]]:
    """Connect, and close on the way out. Commits are the caller's business."""
    with psycopg.connect(database_url()) as conn:
        yield conn

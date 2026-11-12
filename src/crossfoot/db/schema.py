"""Applying the SQL this package ships: `ref`, `er`, and the read-only role.

One loader, because every one of them is an idempotent script read from package data and run in
one transaction. Phase 2 adds files here rather than inventing a second way to apply DDL.
"""

from importlib.resources import files
from typing import Any, Final

import psycopg

REF_SQL: Final = "ref.sql"
ER_SQL: Final = "er.sql"


def sql(name: str) -> str:
    """One of this package's SQL scripts, shipped as package data."""
    return (files("crossfoot.db") / "sql" / name).read_text(encoding="utf-8")


def apply_script(name: str, conn: psycopg.Connection[Any]) -> None:
    """Run one script and commit. Idempotent, so re-running is a no-op."""
    with conn.cursor() as cur:
        cur.execute(sql(name))
    conn.commit()


def apply_er(conn: psycopg.Connection[Any]) -> None:
    """Create the `er` schema and the alias registry table (plan §2.7)."""
    apply_script(ER_SQL, conn)

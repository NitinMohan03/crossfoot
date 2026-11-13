"""The read-only role can read everything the servers need and write nothing (plan §3.3).

This is the layer that makes "every tool is read-only" a property of the database rather than a
property of nobody having written an INSERT yet. `tests/test_mcp/test_no_write.py` adds the
second layer -- the server's own transaction -- and the assertion that no tool advertises a
write.
"""

import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from crossfoot.db.connect import RO_PASSWORD, RO_USER, database_url, readonly_database_url
from crossfoot.db.roles import apply_roles
from crossfoot.db.schema import REF_SQL, apply_er, apply_script


@pytest.fixture(scope="module")
def granted(conn: psycopg.Connection[Any]) -> list[str]:
    # Both schemas are created here, not assumed. On a database nothing has touched yet -- CI's,
    # every run -- `ref` only exists once `test_reference_load.py` has run, and that file sorts
    # after this one. The script is idempotent and loads no rows, so a developer's tables are
    # left as they were.
    apply_script(REF_SQL, conn)
    apply_er(conn)
    return apply_roles(conn)


@pytest.fixture
def ro(granted: list[str]) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(readonly_database_url(), connect_timeout=5) as live:
        yield live


def test_the_grants_cover_every_schema_that_exists(granted: list[str]) -> None:
    # `erp` only appears once a dump has been loaded, so it is not required here.
    assert "ref" in granted
    assert "er" in granted


def test_applying_the_grants_twice_is_a_no_op(
    conn: psycopg.Connection[Any], granted: list[str]
) -> None:
    assert apply_roles(conn) == granted


def test_the_role_can_read_what_the_servers_read(ro: psycopg.Connection[Any]) -> None:
    with ro.cursor() as cur:
        cur.execute("SELECT count(*) FROM ref.fx_rates")
        assert cur.fetchone() is not None
        cur.execute("SELECT count(*) FROM er.counterparty_alias")
        assert cur.fetchone() is not None
        cur.execute("SELECT ref.is_business_day('2024-03-15')")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is True


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO er.counterparty_alias (alias_norm, alias_raw, counterparty_id, provenance, "
        "escalation_id, confirmed_by, confirmed_at) "
        "VALUES ('X', 'X', 1, 'human_confirmed', 'e', 'r', now())",
        "UPDATE er.counterparty_alias SET counterparty_id = 2",
        "DELETE FROM er.counterparty_alias",
        "UPDATE ref.fx_rates SET rate_ppm = 1",
        "TRUNCATE ref.bank_holidays",
    ],
)
def test_the_role_cannot_write(ro: psycopg.Connection[Any], statement: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege), ro.cursor() as cur:
        cur.execute(statement)
    ro.rollback()


def test_the_role_cannot_create_its_own_table_either(ro: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege), ro.cursor() as cur:
        cur.execute("CREATE TABLE er.sneaky (id integer)")
    ro.rollback()


def test_the_readonly_url_is_the_owners_url_with_different_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:secret@db.example:6000/crossfoot")
    for name in ("CROSSFOOT_RO_USER", "CROSSFOOT_RO_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert (
        readonly_database_url() == f"postgresql://{RO_USER}:{RO_PASSWORD}@db.example:6000/crossfoot"
    )
    # Same host, port and database as the owner's URL: one setting moves both.
    assert database_url() == "postgresql://owner:secret@db.example:6000/crossfoot"


def test_the_readonly_credentials_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:secret@db.example:6000/crossfoot")
    monkeypatch.setenv("CROSSFOOT_RO_USER", "reader")
    monkeypatch.setenv("CROSSFOOT_RO_PASSWORD", "p@ss word")
    url = readonly_database_url()
    assert url.startswith("postgresql://reader:p%40ss%20word@")
    assert os.environ["DATABASE_URL"].endswith("/crossfoot")

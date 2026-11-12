"""The database half of the registry's discipline (plan §2.7).

Python refuses to learn while frozen; these tests prove the table itself cannot hold an alias
that did not come from a human-confirmed escalation, so the guarantee survives a bug in the
Python layer.
"""

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from crossfoot.db.schema import apply_er
from crossfoot.entities import PROVENANCE, AliasRegistry, RegistryFrozen

ALIAS = "Alphabet Inc"
CONFIRMED_AT = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def registry_table(conn: psycopg.Connection[Any]) -> None:
    apply_er(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE er.counterparty_alias")
    conn.commit()


def insert(conn: psycopg.Connection[Any], provenance: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO er.counterparty_alias (alias_norm, alias_raw, counterparty_id, "
            "provenance, escalation_id, confirmed_by, confirmed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("ALPHABET", ALIAS, 9, provenance, "esc:0001", "reviewer", CONFIRMED_AT),
        )


def test_a_confirmed_alias_round_trips(conn: psycopg.Connection[Any], registry_table: None) -> None:
    registry = AliasRegistry(frozen=False)
    alias = registry.learn(
        conn,
        alias_raw=ALIAS,
        counterparty_id=9,
        escalation_id="esc:0001",
        confirmed_by="reviewer@example.com",
        confirmed_at=CONFIRMED_AT,
    )
    conn.commit()
    assert alias.alias_norm == "ALPHABET"
    assert registry.lookup("ALPHABET INC") == 9

    # And a fresh load sees it -- frozen, because load defaults to frozen.
    reloaded = AliasRegistry.load(conn)
    assert reloaded.lookup("Alphabet, Inc.") == 9
    assert reloaded.frozen is True
    with pytest.raises(RegistryFrozen):
        reloaded.learn(
            conn,
            alias_raw="Alpha Co",
            counterparty_id=9,
            escalation_id="esc:0002",
            confirmed_by="reviewer@example.com",
        )


def test_an_alias_from_an_llm_decision_is_not_representable(
    conn: psycopg.Connection[Any], registry_table: None
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(conn, "llm_decision")
    conn.rollback()
    # The only value the column accepts is the one the review path writes.
    insert(conn, PROVENANCE)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM er.counterparty_alias")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 1


def test_an_alias_without_an_escalation_is_not_representable(
    conn: psycopg.Connection[Any], registry_table: None
) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO er.counterparty_alias (alias_norm, alias_raw, counterparty_id, "
            "provenance, escalation_id, confirmed_by, confirmed_at) "
            "VALUES (%s, %s, %s, %s, NULL, %s, %s)",
            ("ALPHABET", ALIAS, 9, PROVENANCE, "reviewer", CONFIRMED_AT),
        )
    conn.rollback()


def test_applying_the_schema_twice_is_a_no_op(conn: psycopg.Connection[Any]) -> None:
    apply_er(conn)
    apply_er(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('er.counterparty_alias') IS NOT NULL")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is True

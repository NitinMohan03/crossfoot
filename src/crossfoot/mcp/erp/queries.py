"""The SQL `mcp-erp` runs. Every value is a parameter; the schema is an identifier.

The schema name comes from configuration rather than from a caller, and is checked against an
identifier pattern at startup (`settings.ErpSettings`), but it still goes through
`psycopg.sql.Identifier` here -- composition is the habit, not the exception.

`fx_rate` is `numeric(12,6)` in the dump and parts per million in the canonical schema, so the
conversion happens in SQL where it is exact. Reading a numeric into Python and scaling it would
work too; doing it here keeps the one representation of a rate out of reach of a float.
"""

from collections.abc import Sequence
from datetime import date
from typing import Any, Final

from psycopg import sql

Query = tuple[sql.Composed, tuple[Any, ...]]

INVOICE_COLUMNS: Final = sql.SQL(
    "i.ref, i.counterparty_id, i.issue_date, i.due_date, i.currency, i.amount_minor, "
    "i.base_amount_minor, (i.fx_rate * 1000000)::bigint AS fx_rate_ppm, i.fx_rate_date, "
    "i.created_at, c.legal_name, c.trading_name, c.terms_days"
)

CREDIT_NOTE_COLUMNS: Final = sql.SQL(
    "n.ref, n.counterparty_id, n.issued_on, n.currency, n.amount_minor, n.base_amount_minor, "
    "n.created_at, c.legal_name, c.trading_name, c.terms_days"
)


def _invoice_source(schema: str) -> sql.Composed:
    return sql.SQL(
        "FROM {schema}.invoices i JOIN {schema}.counterparties c ON c.id = i.counterparty_id"
    ).format(schema=sql.Identifier(schema))


def _where(conditions: Sequence[sql.Composable]) -> sql.Composed:
    if not conditions:
        return sql.SQL("").format()
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)


def counterparties(schema: str) -> Query:
    """Every counterparty, for the name index entity resolution scans."""
    statement = sql.SQL(
        "SELECT id, legal_name, trading_name FROM {schema}.counterparties ORDER BY id"
    ).format(schema=sql.Identifier(schema))
    return statement, ()


def invoice_by_ref(schema: str, ref: str) -> Query:
    statement = (
        sql.SQL("SELECT ")
        + INVOICE_COLUMNS
        + sql.SQL(" ")
        + _invoice_source(schema)
        + sql.SQL(" WHERE i.ref = %s")
    )
    return statement, (ref,)


def invoices(
    schema: str,
    *,
    counterparty_id: int | None = None,
    amount_min_minor: int | None = None,
    amount_max_minor: int | None = None,
    issued_from: date | None = None,
    issued_to: date | None = None,
    due_from: date | None = None,
    due_to: date | None = None,
    after_ref: str | None = None,
    limit: int,
) -> Query:
    """Invoices matching every filter given, ordered by ref so paging is keyset-stable."""
    conditions: list[sql.Composable] = []
    params: list[Any] = []
    for clause, value in (
        ("i.counterparty_id = %s", counterparty_id),
        ("i.base_amount_minor >= %s", amount_min_minor),
        ("i.base_amount_minor <= %s", amount_max_minor),
        ("i.issue_date >= %s", issued_from),
        ("i.issue_date <= %s", issued_to),
        ("i.due_date >= %s", due_from),
        ("i.due_date <= %s", due_to),
        ("i.ref > %s", after_ref),
    ):
        if value is not None:
            conditions.append(sql.SQL(clause))
            params.append(value)
    statement = (
        sql.SQL("SELECT ")
        + INVOICE_COLUMNS
        + sql.SQL(" ")
        + _invoice_source(schema)
        + _where(conditions)
        + sql.SQL(" ORDER BY i.ref LIMIT %s")
    )
    return statement, (*params, limit)


def credit_notes(schema: str, *, counterparty_id: int, limit: int) -> Query:
    statement = sql.SQL(
        "SELECT {columns} FROM {schema}.credit_notes n "
        "JOIN {schema}.counterparties c ON c.id = n.counterparty_id "
        "WHERE n.counterparty_id = %s ORDER BY n.ref LIMIT %s"
    ).format(columns=CREDIT_NOTE_COLUMNS, schema=sql.Identifier(schema))
    return statement, (counterparty_id, limit)

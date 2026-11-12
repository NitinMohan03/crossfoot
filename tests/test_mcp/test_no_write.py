"""The agent cannot reach a write through `mcp-erp` (plan §3.3, §4.4).

Four independent assertions, because a single one would be a promise rather than a boundary:

1. the server advertises exactly the read-only allowlist, and says so in its annotations;
2. the role it connects as cannot write (`tests/test_db/test_readonly_role.py` proves this for
   the schemas; here it is proved for the ERP tables themselves);
3. its transactions are read-only, so even a DSN pointing at the owner refuses a write;
4. no write statement exists in the package at all.
"""

import asyncio
from pathlib import Path

import psycopg
import pytest
from mcp import Client

import crossfoot.mcp
from crossfoot.db.connect import database_url
from crossfoot.mcp.common.errors import READ_ONLY, ToolError
from crossfoot.mcp.erp import ErpService, ErpSettings, build_server
from crossfoot.mcp.erp.db import ReadOnlyDatabase
from crossfoot.mcp.erp.server import TOOL_NAMES

WRITE_STATEMENTS = (
    "insert into",
    "update ",
    "delete from",
    "truncate",
    "create table",
    "drop table",
    "alter table",
    "grant ",
)


def test_the_server_advertises_exactly_the_read_only_allowlist(settings: ErpSettings) -> None:
    server = build_server(settings)

    async def listed() -> list[tuple[str, bool | None, bool | None]]:
        async with Client(server) as client:
            result = await client.list_tools()
            return [
                (
                    tool.name,
                    None if tool.annotations is None else tool.annotations.read_only_hint,
                    None if tool.annotations is None else tool.annotations.destructive_hint,
                )
                for tool in result.tools
            ]

    tools = asyncio.run(listed())
    assert tuple(name for name, _, _ in tools) == TOOL_NAMES
    for name, read_only, destructive in tools:
        assert read_only is True, f"{name} is not annotated read-only"
        assert destructive is False, f"{name} is not annotated non-destructive"


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO erp_test.invoices (ref, counterparty_id, issue_date, due_date, currency, "
        "amount_minor, base_amount_minor, created_at) VALUES "
        "('INV-9999-9999', 1, '2024-01-01', '2024-02-01', 'GBP', 1, 1, now())",
        "UPDATE erp_test.invoices SET amount_minor = 1",
        "DELETE FROM erp_test.credit_notes",
        "CREATE TABLE erp_test.sneaky (id integer)",
    ],
)
def test_the_connection_the_server_uses_refuses_every_write(
    service: ErpService, statement: str
) -> None:
    with pytest.raises(ToolError) as caught, service.db.cursor() as cur:
        cur.execute(statement)
    assert caught.value.code == READ_ONLY


def test_the_server_still_answers_after_a_refused_write(service: ErpService) -> None:
    with pytest.raises(ToolError), service.db.cursor() as cur:
        cur.execute("DELETE FROM erp_test.invoices")
    assert service.get_invoice("INV-2024-0001").record_id == "erp:INV-2024-0001"


def test_even_the_owners_credentials_cannot_write_through_this_server(erp_schema: str) -> None:
    # Layer 3 on its own: the owner may write, but the server's transactions are read-only, so
    # a misconfigured DSN still cannot reach a write.
    db = ReadOnlyDatabase(database_url(), connect_timeout=5)
    try:
        with pytest.raises(ToolError) as caught, db.cursor() as cur:
            cur.execute(f"DELETE FROM {erp_schema}.credit_notes")
        assert caught.value.code == READ_ONLY
    finally:
        db.close()


def test_the_role_cannot_write_to_the_erp_tables_outside_the_server_either(
    erp_schema: str, settings: ErpSettings
) -> None:
    with psycopg.connect(settings.dsn, connect_timeout=5) as ro:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), ro.cursor() as cur:
            cur.execute(f"DELETE FROM {erp_schema}.invoices")
        ro.rollback()
        with ro.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {erp_schema}.invoices")
            row = cur.fetchone()
    assert row is not None
    assert row[0] > 0


def test_the_package_contains_no_write_statement() -> None:
    root = Path(crossfoot.mcp.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        lowered = path.read_text(encoding="utf-8").lower()
        offenders += [
            f"{path.relative_to(root).as_posix()}: {statement.strip()}"
            for statement in WRITE_STATEMENTS
            if statement in lowered
        ]
    assert offenders == []

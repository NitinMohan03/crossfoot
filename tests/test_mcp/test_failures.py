"""Bad input and a missing source degrade, they do not crash (plan §4.4 verification).

Both levels are checked: the tool layer raises a coded `ToolError`, and the server returns that
code in the envelope instead of failing the call -- rule 2, which exists because the SDK turns
an unexpected exception into "Error executing tool X" and the agent learns nothing from that.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from mcp import Client

from crossfoot.mcp.common.errors import (
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    NOT_FOUND,
    UNAVAILABLE,
    UNRESOLVED_VENDOR,
    ToolError,
)
from crossfoot.mcp.erp import ErpService, ErpSettings, build_server


def call(server: Any, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """One tool call over an in-process client, returning its structured payload."""

    async def once() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(name, dict(arguments))
            assert result.is_error is False, result.content
            assert result.structured_content is not None
            return dict(result.structured_content)

    return asyncio.run(once())


def error_of(payload: Mapping[str, Any]) -> tuple[str, str]:
    assert "error" in payload, payload
    return payload["error"]["code"], payload["error"]["message"]


# -- the tool layer raises a code ----------------------------------------------------------


def test_an_unknown_ref_is_not_found(service: ErpService) -> None:
    with pytest.raises(ToolError) as caught:
        service.get_invoice("INV-1999-0001")
    assert caught.value.code == NOT_FOUND


def test_an_empty_ref_is_an_invalid_argument(service: ErpService) -> None:
    with pytest.raises(ToolError) as caught:
        service.get_invoice("   ")
    assert caught.value.code == INVALID_ARGUMENT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount_min_minor": 500, "amount_max_minor": 100},
        {"issued_from": "2024-06-01", "issued_to": "2024-01-01"},
        {"due_from": "2024-06-01", "due_to": "2024-01-01"},
    ],
)
def test_a_backwards_range_is_an_invalid_argument(
    service: ErpService, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as caught:
        service.list_open_invoices(**kwargs)
    assert caught.value.code == INVALID_ARGUMENT


@pytest.mark.parametrize("value", ["2024-13-01", "01/03/2024", "yesterday", "2024-02-30"])
def test_an_unparseable_date_is_an_invalid_argument(service: ErpService, value: str) -> None:
    with pytest.raises(ToolError) as caught:
        service.list_open_invoices(issued_from=value)
    assert caught.value.code == INVALID_ARGUMENT


def test_a_limit_below_one_is_an_invalid_argument(service: ErpService) -> None:
    with pytest.raises(ToolError) as caught:
        service.export_invoices(limit=0)
    assert caught.value.code == INVALID_ARGUMENT


def test_a_limit_above_the_maximum_says_what_the_maximum_is(service: ErpService) -> None:
    with pytest.raises(ToolError) as caught:
        service.export_invoices(limit=10_000)
    assert caught.value.code == LIMIT_EXCEEDED
    assert caught.value.extra["max_limit"] == service.settings.max_limit


def test_a_vendor_that_resolves_to_nothing_says_how_close_it_got(service: ErpService) -> None:
    with pytest.raises(ToolError) as caught:
        service.list_open_invoices(vendor="QUAYSIDE LOGISTICS HOLDINGS")
    assert caught.value.code == UNRESOLVED_VENDOR
    assert "best_similarity" in caught.value.extra
    assert "threshold" in caught.value.extra


def test_an_empty_result_is_an_empty_list_not_an_error(service: ErpService) -> None:
    records, _ = service.list_open_invoices(
        issued_from="2023-01-01", issued_to="2023-01-02", limit=10
    )
    assert records == []


def test_a_missing_schema_is_reported_not_raised_as_a_traceback(
    settings: ErpSettings,
) -> None:
    absent = ErpSettings(dsn=settings.dsn, schema="erp_does_not_exist")
    service = ErpService(absent)
    try:
        with pytest.raises(ToolError) as caught:
            service.export_invoices(limit=1)
        assert caught.value.code == NOT_FOUND
        assert "db-load" in caught.value.message
    finally:
        service.close()


def test_an_unreachable_database_is_unavailable(settings: ErpSettings) -> None:
    dead = ErpSettings(
        dsn="postgresql://crossfoot_ro:crossfoot_ro@127.0.0.1:1/crossfoot",
        schema=settings.schema,
        connect_timeout=2,
    )
    service = ErpService(dead)
    try:
        with pytest.raises(ToolError) as caught:
            service.export_invoices(limit=1)
        assert caught.value.code == UNAVAILABLE
        # The DSN carries a password and must not appear in what the agent is told.
        assert "crossfoot_ro" not in caught.value.message
    finally:
        service.close()


def test_a_schema_name_that_is_not_an_identifier_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="not a usable schema name"):
        ErpSettings(dsn="postgresql://x/y", schema="erp; DROP SCHEMA public")


# -- the server returns that code, and keeps working ----------------------------------------


def test_the_server_returns_the_envelope_instead_of_failing_the_call(
    settings: ErpSettings,
) -> None:
    server = build_server(settings)
    assert error_of(call(server, "get_invoice", {"ref": "INV-1999-0001"}))[0] == NOT_FOUND
    assert (
        error_of(call(server, "list_open_invoices", {"issued_from": "nope"}))[0] == INVALID_ARGUMENT
    )
    assert (
        error_of(call(server, "get_credit_notes", {"vendor": "NOT A COMPANY AT ALL"}))[0]
        == UNRESOLVED_VENDOR
    )
    assert error_of(call(server, "export_invoices", {"limit": 10_000}))[0] == LIMIT_EXCEEDED


def test_a_failed_call_does_not_poison_the_session(settings: ErpSettings) -> None:
    server = build_server(settings)

    async def sequence() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(server) as client:
            bad = await client.call_tool("get_invoice", {"ref": "INV-1999-0001"})
            good = await client.call_tool("get_invoice", {"ref": "INV-2024-0001"})
            assert bad.structured_content is not None
            assert good.structured_content is not None
            return dict(bad.structured_content), dict(good.structured_content)

    bad, good = asyncio.run(sequence())
    assert error_of(bad)[0] == NOT_FOUND
    assert good["record_id"] == "erp:INV-2024-0001"

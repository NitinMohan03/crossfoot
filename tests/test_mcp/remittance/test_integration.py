"""`mcp-remittance` started as a real process, spoken to over real stdio (plan §4.4 verification).

The one test that exercises the transport. What is proved here is the wiring end to end -- a
subprocess, an initialize handshake, the advertised tool list, configuration through the
environment, PDFs read from disk and names read from Postgres -- and that what comes back over
the wire parses into `CanonicalRecord` with ids the ground-truth manifest knows.

The dataset is the developed advices (see `conftest.py`); the held-out layout is measured in
`test_held_out.py`.
"""

import asyncio
import os
import sys
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from crossfoot.mcp.common.errors import INVALID_ARGUMENT, NOT_FOUND
from crossfoot.mcp.remittance.server import TOOL_NAMES
from crossfoot.mcp.remittance.settings import (
    DATA_ENV,
    DSN_ENV,
    ERP_SCHEMA_ENV,
    RemittanceSettings,
)
from crossfoot.models.canonical import CanonicalRecord, RemittanceDetail
from tests.test_mcp.remittance.conftest import DEVELOPED

TIMEOUT = 60.0
PAGE = 200


def server_parameters(settings: RemittanceSettings) -> StdioServerParameters:
    """`python -m crossfoot.mcp.remittance`, configured through the environment."""
    env = dict(os.environ)
    env[DATA_ENV] = str(settings.data_dir)
    env[DSN_ENV] = settings.dsn
    env[ERP_SCHEMA_ENV] = settings.erp_schema
    return StdioServerParameters(
        command=sys.executable, args=["-m", "crossfoot.mcp.remittance"], env=env
    )


async def _session(settings: RemittanceSettings) -> dict[str, Any]:
    async with Client(server_parameters(settings), read_timeout_seconds=TIMEOUT) as client:
        out: dict[str, Any] = {"listed": await client.list_tools()}
        out["advice"] = await client.call_tool(
            "get_remittance_for_payment", {"payment_ref": "PAY-2024-0004"}
        )
        out["euro"] = await client.call_tool(
            "get_remittance_for_payment", {"payment_ref": "PAY-2024-0027"}
        )
        out["missing"] = await client.call_tool(
            "get_remittance_for_payment", {"payment_ref": "PAY-2024-9999"}
        )
        out["invalid"] = await client.call_tool(
            "get_remittance_lines", {"remittance_id": "INV-2024-0027"}
        )
        out["lines"] = await client.call_tool(
            "get_remittance_lines", {"remittance_id": "remit:RA-2024-0004"}
        )
        # The whole export, paged the way ingestion pages it, on the same session.
        pages: list[Any] = []
        after: str | None = None
        while True:
            arguments: dict[str, Any] = {"limit": PAGE}
            if after is not None:
                arguments["after_id"] = after
            page = await client.call_tool("export_remittances", arguments)
            pages.append(page)
            content = page.structured_content or {}
            after = content.get("next_after_id")
            if not content.get("has_more"):
                break
        out["pages"] = pages
        return out


@pytest.fixture(scope="module")
def conversation(remittance_settings: RemittanceSettings) -> dict[str, Any]:
    """One subprocess, one session, every call -- as an agent would use it."""
    return asyncio.run(_session(remittance_settings))


def test_the_server_starts_and_advertises_its_tools(conversation: dict[str, Any]) -> None:
    listed = conversation["listed"]
    assert tuple(tool.name for tool in listed.tools) == TOOL_NAMES
    # Each tool describes itself, because the description is what the agent reads.
    assert all(tool.description for tool in listed.tools)
    assert all(tool.annotations.read_only_hint is True for tool in listed.tools)


def test_a_record_from_the_wire_is_a_canonical_record(
    conversation: dict[str, Any], group_of: dict[str, dict[str, Any]]
) -> None:
    result = conversation["advice"]
    assert result.is_error is False
    payload = result.structured_content
    record = CanonicalRecord.model_validate(payload)
    assert record.record_id == "remit:RA-2024-0004"
    assert (record.source, record.role) == ("remittance", "advice")
    assert record.record_id in group_of
    # Minor units arrived as integers, not as a rounded decimal in a string.
    assert payload["amount"]["minor"] == 871_753
    assert isinstance(payload["amount"]["minor"], int)
    assert payload["record_date"] == "2024-01-19"
    # The header and its lines crossed the wire as one record.
    assert isinstance(record.detail, RemittanceDetail)
    assert [line["amount_minor"] for line in payload["detail"]["lines"]] == [
        83_600,
        877_432,
        -89_279,
    ]
    assert payload["detail"]["lines"][2] == {
        "invoice_ref": "CN-2024-0001",
        "amount_minor": -89_279,
        "note": "credit note",
    }
    # The party was resolved against names the subprocess read from Postgres.
    assert (record.party.counterparty_id, record.party.method) == (60, "exact")


def test_a_foreign_advice_crosses_the_wire_with_its_rate_beside_it(
    conversation: dict[str, Any],
) -> None:
    amount = conversation["euro"].structured_content["amount"]
    assert amount == {
        "minor": 362_936,
        "currency": "EUR",
        "base_minor": 316_029,
        "fx_rate_ppm": 1_148_426,
        "fx_rate_date": "2024-02-16",
    }


def test_the_lines_tool_answers_over_the_wire(conversation: dict[str, Any]) -> None:
    payload = conversation["lines"].structured_content
    assert payload == {
        "remittance_id": "remit:RA-2024-0004",
        "payment_ref": "PAY-2024-0004",
        "currency": "GBP",
        "total_minor": 871_753,
        "lines": [
            {"invoice_ref": "INV-2024-0027", "amount_minor": 83_600},
            {"invoice_ref": "INV-2024-0028", "amount_minor": 877_432},
            {"invoice_ref": "CN-2024-0001", "amount_minor": -89_279, "note": "credit note"},
        ],
        "count": 3,
    }


def test_the_whole_export_lands_in_the_canonical_schema_and_the_manifest(
    conversation: dict[str, Any], developed: list[dict[str, Any]]
) -> None:
    pages = [page.structured_content for page in conversation["pages"]]
    assert all(page.is_error is False for page in conversation["pages"])
    assert [(page["count"], page["examined"], page["has_more"]) for page in pages] == [
        (200, 200, True),
        (200, 200, True),
        (44, 44, False),
    ]
    assert all(page["unparsed"] == [] for page in pages)
    records = [CanonicalRecord.model_validate(item) for page in pages for item in page["records"]]
    assert len(records) == DEVELOPED
    assert all(record.source == "remittance" and record.role == "advice" for record in records)
    # Exactly the developed advices ground truth lists, in its order, total for total.
    assert [(record.record_id, record.amount.minor) for record in records] == [
        (row["id"], row["total_minor"]) for row in developed
    ]
    assert pages[0]["next_after_id"] == records[199].record_id


def test_an_error_crosses_the_wire_as_a_code_and_the_session_survives(
    conversation: dict[str, Any],
) -> None:
    missing, invalid = conversation["missing"], conversation["invalid"]
    # Not failed calls: calls that answered with a code the agent can branch on.
    assert missing.is_error is False
    assert invalid.is_error is False
    assert missing.structured_content["error"]["code"] == NOT_FOUND
    assert invalid.structured_content["error"]["code"] == INVALID_ARGUMENT
    # Everything after them, on the same session and the same process, still worked.
    assert conversation["lines"].structured_content["count"] == 3
    assert len(conversation["pages"]) == 3

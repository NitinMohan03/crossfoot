"""`mcp-bank` started as a real process, spoken to over real stdio (plan §4.4 verification).

The one test that exercises the transport. What is proved here is the wiring end to end -- a
subprocess, an initialize handshake, the advertised tool list, configuration through the
environment, files read from disk and names read from Postgres -- and that what comes back over
the wire parses into `CanonicalRecord` with ids the ground-truth manifest knows.
"""

import asyncio
import os
import sys
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from crossfoot.mcp.bank.server import TOOL_NAMES
from crossfoot.mcp.bank.settings import DATA_ENV, DSN_ENV, ERP_SCHEMA_ENV, BankSettings
from crossfoot.mcp.common.errors import INVALID_ARGUMENT, NOT_FOUND
from crossfoot.models.canonical import CanonicalRecord
from tests.test_mcp.bank.conftest import LINES

TIMEOUT = 30.0
PAGE = 200


def server_parameters(settings: BankSettings) -> StdioServerParameters:
    """`python -m crossfoot.mcp.bank`, configured through the environment, as a client would."""
    env = dict(os.environ)
    env[DATA_ENV] = str(settings.data_dir)
    env[DSN_ENV] = settings.dsn
    env[ERP_SCHEMA_ENV] = settings.erp_schema
    return StdioServerParameters(command=sys.executable, args=["-m", "crossfoot.mcp.bank"], env=env)


async def _session(settings: BankSettings) -> dict[str, Any]:
    async with Client(server_parameters(settings), read_timeout_seconds=TIMEOUT) as client:
        out: dict[str, Any] = {"listed": await client.list_tools()}
        out["line"] = await client.call_tool("get_bank_line", {"id": "bank:2024-03-01:000001"})
        out["summer"] = await client.call_tool("get_bank_line", {"id": "bank:2024-04-02:000003"})
        out["missing"] = await client.call_tool("get_bank_line", {"id": "bank:2024-03-01:000999"})
        out["invalid"] = await client.call_tool("find_bank_lines", {"date_from": "03/06/2024"})
        out["day"] = await client.call_tool(
            "find_bank_lines", {"date_from": "2024-06-03", "date_to": "2024-06-03"}
        )
        out["refunds"] = await client.call_tool("find_bank_lines", {"amount_max_minor": -1})
        # The whole export, paged the way ingestion pages it, on the same session.
        pages: list[Any] = []
        after: str | None = None
        while True:
            arguments: dict[str, Any] = {"limit": PAGE}
            if after is not None:
                arguments["after_id"] = after
            page = await client.call_tool("export_bank_lines", arguments)
            pages.append(page)
            content = page.structured_content or {}
            after = content.get("next_after_id")
            if not content.get("records"):
                break
        out["pages"] = pages
        return out


@pytest.fixture(scope="module")
def conversation(bank_settings: BankSettings) -> dict[str, Any]:
    """One subprocess, one session, every call -- as an agent would use it."""
    return asyncio.run(_session(bank_settings))


def test_the_server_starts_and_advertises_its_tools(conversation: dict[str, Any]) -> None:
    listed = conversation["listed"]
    assert tuple(tool.name for tool in listed.tools) == TOOL_NAMES
    # Each tool describes itself, because the description is what the agent reads.
    assert all(tool.description for tool in listed.tools)
    assert all(tool.annotations.read_only_hint is True for tool in listed.tools)


def test_a_record_from_the_wire_is_a_canonical_record(
    conversation: dict[str, Any], manifest_bank_ids: set[str]
) -> None:
    result = conversation["line"]
    assert result.is_error is False
    payload = result.structured_content
    record = CanonicalRecord.model_validate(payload)
    assert record.record_id == "bank:2024-03-01:000001"
    assert record.source == "bank"
    assert record.record_id in manifest_bank_ids
    # Minor units arrived as integers, not as a rounded decimal in a string.
    assert payload["amount"]["minor"] == 249_230
    assert isinstance(payload["amount"]["minor"], int)
    # The party was resolved against names the subprocess read from Postgres.
    assert (record.party.counterparty_id, record.party.method) == (50, "normalised")


def test_utc_and_the_original_text_both_cross_the_wire(conversation: dict[str, Any]) -> None:
    payload = conversation["summer"].structured_content
    assert payload["event_time"] == "2024-04-01T23:02:00Z"
    assert payload["event_time_source"] == "2024-04-02T00:02:00+01:00"
    assert payload["record_date"] == "2024-04-02"
    record = CanonicalRecord.model_validate(payload)
    assert record.event_time is not None
    assert record.event_time.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_the_whole_export_lands_in_the_canonical_schema_and_the_manifest(
    conversation: dict[str, Any], manifest_bank_ids: set[str]
) -> None:
    pages = [page.structured_content for page in conversation["pages"]]
    assert all(page.is_error is False for page in conversation["pages"])
    # Six full pages, a seventh of ten, and the empty one that ends the walk.
    assert [page["count"] for page in pages] == [200, 200, 200, 200, 200, 200, 10, 0]
    records = [CanonicalRecord.model_validate(item) for page in pages for item in page["records"]]
    assert len(records) == LINES
    assert all(record.source == "bank" and record.role == "payment" for record in records)
    ids = [record.record_id for record in records]
    assert ids == sorted(ids)
    # Every id is one ground truth knows, and ground truth knows no bank id that did not come.
    assert set(ids) == manifest_bank_ids
    assert pages[0]["next_after_id"] == records[199].record_id


def test_known_queries_answer_correctly_over_the_wire(conversation: dict[str, Any]) -> None:
    day = conversation["day"].structured_content
    assert (day["count"], day["total_matches"]) == (8, 8)
    assert {item["record_date"] for item in day["records"]} == {"2024-06-03"}
    refunds = conversation["refunds"].structured_content
    assert (refunds["count"], refunds["total_matches"]) == (10, 10)
    assert all(item["amount"]["minor"] < 0 for item in refunds["records"])
    assert {item["detail"]["txn_type"] for item in refunds["records"]} == {"FPO"}


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
    assert conversation["day"].structured_content["count"] == 8
    assert len(conversation["pages"]) == 8

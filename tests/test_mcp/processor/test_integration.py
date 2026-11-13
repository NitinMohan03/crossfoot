"""`mcp-processor` started as a real process, talking to the mock API as another real process.

The one test that exercises both transports at once (plan §4.4 verification): the API is
`python -m crossfoot.mock_processor` listening on a real port, the server is
`python -m crossfoot.mcp.processor` spoken to over real stdio, and between them is a real socket
carrying a real bearer token. What is proved here is the wiring end to end -- two subprocesses,
an initialize handshake, the advertised tool list, configuration through the environment, names
read from Postgres -- and that what comes back over the wire parses into `CanonicalRecord` with
ids the ground-truth manifest knows.

A second server, started with the wrong token, shows the failure that only exists at this
level: a credential the API refuses, reported as a code, by a process that is still up.
"""

import asyncio
import os
import subprocess
import sys
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from crossfoot.mcp.common.errors import INVALID_ARGUMENT, NOT_FOUND, UNAVAILABLE
from crossfoot.mcp.processor.server import TOOL_NAMES
from crossfoot.mcp.processor.settings import (
    DSN_ENV,
    ERP_SCHEMA_ENV,
    TOKEN_ENV,
    URL_ENV,
    ProcessorSettings,
)
from crossfoot.models.canonical import CanonicalRecord
from tests.test_mcp.processor.conftest import BATCHES, FIRST_ID, SETTLEMENTS, TOKEN, USD_ID

TIMEOUT = 30.0
PAGE = 200
WRONG_TOKEN = "not-the-token-the-api-wants"


def server_parameters(settings: ProcessorSettings, url: str, token: str) -> StdioServerParameters:
    """`python -m crossfoot.mcp.processor`, configured through the environment only."""
    env = dict(os.environ)
    env[URL_ENV] = url
    env[TOKEN_ENV] = token
    env[DSN_ENV] = settings.dsn
    env[ERP_SCHEMA_ENV] = settings.erp_schema
    return StdioServerParameters(
        command=sys.executable, args=["-m", "crossfoot.mcp.processor"], env=env
    )


async def _walk(client: Client, tool: str, key: str) -> list[Any]:
    """A whole export, paged the way ingestion pages it."""
    pages: list[Any] = []
    cursor: str | None = None
    while True:
        arguments: dict[str, Any] = {"limit": PAGE}
        if cursor is not None:
            arguments["cursor"] = cursor
        page = await client.call_tool(tool, arguments)
        pages.append(page)
        content = page.structured_content or {}
        cursor = content.get("next_cursor")
        if not content.get("has_more") or not content.get(key):
            return pages


async def _session(settings: ProcessorSettings, url: str) -> dict[str, Any]:
    parameters = server_parameters(settings, url, TOKEN)
    async with Client(parameters, read_timeout_seconds=TIMEOUT) as client:
        out: dict[str, Any] = {"listed": await client.list_tools()}
        out["line"] = await client.call_tool("get_settlement", {"id": FIRST_ID})
        out["foreign"] = await client.call_tool("get_settlement", {"id": USD_ID})
        out["missing"] = await client.call_tool("get_settlement", {"id": "proc:stl_000000000000"})
        out["invalid"] = await client.call_tool("list_settlements", {"date_from": "03/06/2024"})
        out["day"] = await client.call_tool(
            "list_settlements", {"date_from": "2024-06-03", "date_to": "2024-06-03"}
        )
        # The id exactly as the bank's `TFR` line writes it.
        out["batch"] = await client.call_tool("get_batch", {"id": "BAT_2024-07-17_01"})
        out["settlements"] = await _walk(client, "export_settlements", "records")
        out["batches"] = await _walk(client, "export_batches", "batches")
        return out


async def _refused(settings: ProcessorSettings, url: str) -> dict[str, Any]:
    parameters = server_parameters(settings, url, WRONG_TOKEN)
    async with Client(parameters, read_timeout_seconds=TIMEOUT) as client:
        return {
            "listed": await client.list_tools(),
            "first": await client.call_tool("get_settlement", {"id": FIRST_ID}),
            "second": await client.call_tool("export_batches", {"limit": 1}),
            "argument": await client.call_tool("get_settlement", {"id": "nonsense"}),
        }


@pytest.fixture(scope="module")
def conversation(processor_settings: ProcessorSettings, live_api: str) -> dict[str, Any]:
    """Two subprocesses, one session, every call -- as an agent would use it."""
    return asyncio.run(_session(processor_settings, live_api))


@pytest.fixture(scope="module")
def refusal(processor_settings: ProcessorSettings, live_api: str) -> dict[str, Any]:
    return asyncio.run(_refused(processor_settings, live_api))


def test_the_server_starts_and_advertises_its_tools(conversation: dict[str, Any]) -> None:
    listed = conversation["listed"]
    assert tuple(tool.name for tool in listed.tools) == TOOL_NAMES
    # Each tool describes itself, because the description is what the agent reads.
    assert all(tool.description for tool in listed.tools)
    assert all(tool.annotations.read_only_hint is True for tool in listed.tools)


def test_a_record_from_the_wire_is_a_canonical_record(
    conversation: dict[str, Any], manifest_proc_ids: set[str]
) -> None:
    result = conversation["line"]
    assert result.is_error is False
    payload = result.structured_content
    record = CanonicalRecord.model_validate(payload)
    assert record.record_id == FIRST_ID
    assert (record.source, record.role) == ("processor", "payment")
    assert record.record_id in manifest_proc_ids
    # Minor units arrived as integers, not as a rounded decimal in a string.
    assert payload["amount"]["minor"] == 289_375
    assert isinstance(payload["amount"]["minor"], int)
    assert (payload["detail"]["fee_minor"], payload["detail"]["net_minor"]) == (4_109, 285_266)
    # The party was resolved against names the subprocess read from Postgres.
    assert (record.party.counterparty_id, record.party.method) == (28, "exact")
    # The date stayed a date, the moment is UTC, and the source's own text came with it.
    assert payload["record_date"] == "2024-01-11"
    assert payload["event_time"] == "2024-01-10T19:05:39Z"
    assert payload["event_time_source"] == "2024-01-10T19:05:39Z"


def test_a_foreign_amount_crosses_the_wire_with_its_integer_rate(
    conversation: dict[str, Any],
) -> None:
    payload = conversation["foreign"].structured_content
    assert payload["amount"] == {
        "minor": 3454,
        "currency": "USD",
        "base_minor": 2591,
        "fx_rate_ppm": 1_333_209,
        "fx_rate_date": "2024-08-21",
    }
    CanonicalRecord.model_validate(payload)


def test_the_whole_export_lands_in_the_canonical_schema_and_the_manifest(
    conversation: dict[str, Any], manifest_proc_ids: set[str]
) -> None:
    assert all(page.is_error is False for page in conversation["settlements"])
    pages = [page.structured_content for page in conversation["settlements"]]
    assert [page["count"] for page in pages] == [200, 200, 152]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert pages[-1]["next_cursor"] is None
    records = [CanonicalRecord.model_validate(item) for page in pages for item in page["records"]]
    assert len(records) == SETTLEMENTS
    assert all(record.source == "processor" and record.role == "payment" for record in records)
    # Every id is one ground truth knows, and ground truth knows no processor id that did not
    # come.
    assert {record.record_id for record in records} == manifest_proc_ids
    keys = [(record.record_date, record.record_id) for record in records]
    assert keys == sorted(keys)


def test_every_batch_comes_through_the_bulk_read(conversation: dict[str, Any]) -> None:
    pages = [page.structured_content for page in conversation["batches"]]
    assert [page["count"] for page in pages] == [200, 68]
    batches = [batch for page in pages for batch in page["batches"]]
    assert len({batch["id"] for batch in batches}) == BATCHES
    assert sum(batch["count"] for batch in batches) == SETTLEMENTS
    assert all(isinstance(batch["payout_base_minor"], int) for batch in batches)


def test_known_queries_answer_correctly_over_the_wire(conversation: dict[str, Any]) -> None:
    day = conversation["day"].structured_content
    assert (day["count"], day["has_more"]) == (2, False)
    assert {item["record_date"] for item in day["records"]} == {"2024-06-03"}

    found = conversation["batch"].structured_content
    assert found["batch"] == {
        "id": "bat_2024-07-17_01",
        "closed_on": "2024-07-17",
        "paid_out_on": "2024-07-18",
        "currency": "GBP",
        "count": 8,
        "total_net_minor": 2_754_479,
        "payout_base_minor": 2_754_479,
    }
    assert (found["count"], found["total_lines"]) == (8, 8)
    assert sum(item["detail"]["net_minor"] for item in found["records"]) == 2_754_479


def test_an_error_crosses_the_wire_as_a_code_and_the_session_survives(
    conversation: dict[str, Any],
) -> None:
    missing, invalid = conversation["missing"], conversation["invalid"]
    # Not failed calls: calls that answered with a code the agent can branch on.
    assert missing.is_error is False
    assert invalid.is_error is False
    assert missing.structured_content["error"]["code"] == NOT_FOUND
    assert invalid.structured_content["error"]["code"] == INVALID_ARGUMENT
    # Everything after them, on the same session and the same two processes, still worked.
    assert conversation["day"].structured_content["count"] == 2
    assert len(conversation["settlements"]) == 3


def test_a_refused_token_is_a_code_from_a_server_that_is_still_up(
    refusal: dict[str, Any],
) -> None:
    # The server starts and lists its tools: a bad credential is found out by asking.
    assert tuple(tool.name for tool in refusal["listed"].tools) == TOOL_NAMES
    for name in ("first", "second"):
        result = refusal[name]
        assert result.is_error is False
        error = result.structured_content["error"]
        assert error["code"] == UNAVAILABLE
        assert (error["upstream_status"], error["upstream_code"]) == (401, "invalid_token")
        assert error["retryable"] is False
        assert TOKEN_ENV in error["message"]
    # Still the same process, still answering -- and an argument error is still an argument
    # error, not a symptom of the credential.
    assert refusal["argument"].structured_content["error"]["code"] == INVALID_ARGUMENT
    # Neither token is anywhere in what the agent was told.
    told = str([refusal[name].structured_content for name in ("first", "second", "argument")])
    assert WRONG_TOKEN not in told
    assert TOKEN not in told


@pytest.mark.parametrize(
    ("arguments", "token", "expected"),
    [
        # A URL the server will not start with, and a perfectly good secret beside it.
        (["--url", "ftp://processor.example"], "s3cr3t-live-token", "not an http(s) URL"),
        # A token that cannot be a bearer token: refused by name, not by value.
        ([], "s3cr3t with-a-space", TOKEN_ENV),
    ],
)
def test_a_server_that_refuses_to_start_does_not_print_the_token(
    arguments: list[str], token: str, expected: str
) -> None:
    env = dict(os.environ)
    env[TOKEN_ENV] = token
    finished = subprocess.run(
        [sys.executable, "-m", "crossfoot.mcp.processor", *arguments],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert finished.returncode != 0
    printed = finished.stdout + finished.stderr
    assert expected in printed
    assert "s3cr3t" not in printed

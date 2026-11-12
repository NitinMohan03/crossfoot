"""`mcp-erp` started as a real process, spoken to over real stdio (plan §4.4 verification).

This is the one test that exercises the transport, because rule 1 keeps every decision in the
tool layer: what is proved here is that the wiring works end to end -- a subprocess, an
initialize handshake, the advertised tool list, and payloads that parse back into
`CanonicalRecord` and match ids the ground-truth manifest knows.
"""

import asyncio
import os
import sys
from collections.abc import Mapping
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from crossfoot.mcp.common.errors import NOT_FOUND
from crossfoot.mcp.erp.server import TOOL_NAMES
from crossfoot.mcp.erp.settings import ErpSettings
from crossfoot.models.canonical import CanonicalRecord

TIMEOUT = 30.0


def server_parameters(settings: ErpSettings) -> StdioServerParameters:
    """`python -m crossfoot.mcp.erp`, configured through the environment, as a client would."""
    env = dict(os.environ)
    env["CROSSFOOT_ERP_SCHEMA"] = settings.schema
    env["CROSSFOOT_ERP_DSN"] = settings.dsn
    return StdioServerParameters(command=sys.executable, args=["-m", "crossfoot.mcp.erp"], env=env)


async def _session(settings: ErpSettings, calls: list[tuple[str, Mapping[str, Any]]]) -> Any:
    async with Client(server_parameters(settings), read_timeout_seconds=TIMEOUT) as client:
        listed = await client.list_tools()
        results = [await client.call_tool(name, dict(args)) for name, args in calls]
        return listed, results


def talk(settings: ErpSettings, calls: list[tuple[str, Mapping[str, Any]]]) -> Any:
    return asyncio.run(_session(settings, calls))


@pytest.fixture(scope="module")
def conversation(settings: ErpSettings) -> Any:
    """One subprocess, one session, every call -- as an agent would use it."""
    return talk(
        settings,
        [
            ("get_invoice", {"ref": "INV-2024-0001"}),
            ("export_invoices", {"limit": 25}),
            ("get_invoice", {"ref": "INV-1999-0001"}),
            ("lookup_alias", {"name": "QUAYSIDE LOGISTICS HOLDINGS"}),
            ("export_invoices", {"limit": 5}),
        ],
    )


def test_the_server_starts_and_advertises_its_tools(conversation: Any) -> None:
    listed, _ = conversation
    assert tuple(tool.name for tool in listed.tools) == TOOL_NAMES
    # Each tool describes itself, because the description is what the agent reads.
    assert all(tool.description for tool in listed.tools)


def test_a_record_from_the_wire_is_a_canonical_record(
    conversation: Any, manifest_erp_ids: set[str]
) -> None:
    _, results = conversation
    payload = results[0].structured_content
    assert results[0].is_error is False
    record = CanonicalRecord.model_validate(payload)
    assert record.record_id == "erp:INV-2024-0001"
    assert record.source == "erp"
    assert record.record_id in manifest_erp_ids
    # Minor units arrived as integers, not as a rounded decimal in a string.
    assert isinstance(payload["amount"]["minor"], int)


def test_every_exported_record_lands_in_the_canonical_schema_and_the_manifest(
    conversation: Any, manifest_erp_ids: set[str]
) -> None:
    _, results = conversation
    payload = results[1].structured_content
    assert payload["count"] == 25
    records = [CanonicalRecord.model_validate(item) for item in payload["records"]]
    assert len(records) == 25
    assert all(record.source == "erp" for record in records)
    assert {record.record_id for record in records} <= manifest_erp_ids
    assert payload["next_after_ref"] == records[-1].reference


def test_an_error_crosses_the_wire_as_a_code_and_the_session_survives(conversation: Any) -> None:
    _, results = conversation
    failed = results[2]
    # Not a failed call: a call that answered with a code the agent can branch on.
    assert failed.is_error is False
    assert failed.structured_content is not None
    assert failed.structured_content["error"]["code"] == NOT_FOUND
    # The two calls after it, on the same session and the same process, still worked.
    assert results[3].structured_content["party"]["method"] == "unresolved"
    assert results[4].structured_content["count"] == 5


def test_the_alias_tool_reports_the_registry_state_over_the_wire(conversation: Any) -> None:
    _, results = conversation
    payload = results[3].structured_content
    assert payload["registry_frozen"] is True
    assert payload["registry_available"] is True

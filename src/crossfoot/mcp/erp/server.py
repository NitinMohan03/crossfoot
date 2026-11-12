"""Registration, serialisation, and nothing else. Rule 1.

Every tool here is three lines: call the service, turn the result into a payload, and hand a
`ToolError` back in the envelope. There is no logic to test at this layer, which is why one
integration test over real stdio is enough and the tool tests talk to `ErpService` directly.

Every tool is annotated `readOnlyHint=True`, and `TOOL_NAMES` is the allowlist
`tests/test_mcp/test_no_write.py` compares the advertised list against: a tool that writes
cannot be added here without that test failing.
"""

from collections.abc import Callable
from typing import Any, Final

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from crossfoot.mcp.common.errors import ToolError
from crossfoot.mcp.common.results import party_payload, record_payload, records_payload
from crossfoot.mcp.erp.settings import ErpSettings
from crossfoot.mcp.erp.tools import NO_STATUS_NOTE, ErpService

SERVER_NAME: Final = "mcp-erp"
VERSION: Final = "1.0.0"

#: Every tool this server has, all of them reads. Asserted against the live server in tests.
TOOL_NAMES: Final = (
    "get_invoice",
    "list_open_invoices",
    "get_credit_notes",
    "lookup_alias",
    "export_invoices",
)

READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False
)

INSTRUCTIONS: Final = (
    "The ERP: invoices and credit notes, read-only. Amount filters are GBP minor units and "
    "match an invoice's base amount, so one window works across currencies. "
    f"Note that {NO_STATUS_NOTE}. "
    "A failed call answers with an `error` object carrying a stable `code` rather than failing "
    "the call, so branch on `code`."
)

Payload = dict[str, Any]


def _guard(call: Callable[[], Payload]) -> Payload:
    """Run a tool body, returning the error envelope instead of raising (rule 2)."""
    try:
        return call()
    except ToolError as exc:
        return exc.payload()


def build_server(
    settings: ErpSettings | None = None, service: ErpService | None = None
) -> MCPServer:
    """The ERP MCP server. `service` is injectable so tests can drive it without a transport."""
    resolved = settings or ErpSettings.from_env()
    erp = service or ErpService(resolved)
    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=VERSION,
        instructions=INSTRUCTIONS,
    )

    @server.tool(annotations=READ_ONLY)
    def get_invoice(ref: str) -> Payload:
        """One invoice by ERP ref, e.g. `INV-2024-0891`."""
        return _guard(lambda: record_payload(erp.get_invoice(ref)))

    @server.tool(annotations=READ_ONLY)
    def list_open_invoices(
        vendor: str | None = None,
        amount_min_minor: int | None = None,
        amount_max_minor: int | None = None,
        issued_from: str | None = None,
        issued_to: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        limit: int | None = None,
    ) -> Payload:
        """Invoices for a vendor and/or an amount (GBP minor units) and date window.

        `vendor` is free text and goes through entity resolution; the counterparty it resolved to
        comes back in `party`, so a near-miss is visible rather than silent.
        """

        def body() -> Payload:
            records, party = erp.list_open_invoices(
                vendor=vendor,
                amount_min_minor=amount_min_minor,
                amount_max_minor=amount_max_minor,
                issued_from=issued_from,
                issued_to=issued_to,
                due_from=due_from,
                due_to=due_to,
                limit=limit,
            )
            extra: Payload = {"note": NO_STATUS_NOTE}
            if party is not None:
                extra["party"] = party.model_dump(mode="json", exclude_none=True)
            return records_payload(records, **extra)

        return _guard(body)

    @server.tool(annotations=READ_ONLY)
    def get_credit_notes(vendor: str, limit: int | None = None) -> Payload:
        """Credit notes for one vendor. Amounts are negative: they reduce what is owed."""

        def body() -> Payload:
            records, party = erp.get_credit_notes(vendor, limit=limit)
            return records_payload(records, party=party.model_dump(mode="json", exclude_none=True))

        return _guard(body)

    @server.tool(annotations=READ_ONLY)
    def lookup_alias(name: str) -> Payload:
        """Resolve a counterparty name, reporting how it was identified.

        `method` is exact, normalised, alias, fuzzy or unresolved. The registry is frozen: this
        tool never records anything, and an alias is learned only from a confirmed escalation.
        """
        return _guard(
            lambda: party_payload(
                erp.lookup_alias(name),
                registry_available=erp.registry_available,
                registry_frozen=True,
            )
        )

    @server.tool(annotations=READ_ONLY)
    def export_invoices(after_ref: str | None = None, limit: int | None = None) -> Payload:
        """Invoices in ref order for bulk ingestion. Page with `after_ref` = the last ref seen."""

        def body() -> Payload:
            records = erp.export_invoices(after_ref=after_ref, limit=limit)
            last = records[-1].reference if records else None
            return records_payload(records, next_after_ref=last)

        return _guard(body)

    return server


def run() -> None:  # pragma: no cover - exercised by the stdio integration test
    """Serve on stdio. What `just mcp-erp` and `python -m crossfoot.mcp.erp` run."""
    build_server().run("stdio")

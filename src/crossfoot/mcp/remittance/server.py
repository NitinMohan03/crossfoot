"""Registration, serialisation, and nothing else. Rule 1.

Every tool here is a few lines: call the service, turn the result into a payload, and let
`common.guard` hand a `ToolError` back in the envelope. There is no logic to test at this layer,
which is why one integration test over real stdio is enough and the tool tests talk to
`RemittanceService` directly.

Every tool is annotated `readOnlyHint=True`, and `TOOL_NAMES` is the allowlist
`tests/test_mcp/remittance/test_no_write.py` compares the advertised list against: a tool that
writes cannot be added here without that test failing.
"""

from typing import Final

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from crossfoot.mcp.common.errors import Payload, guard
from crossfoot.mcp.common.results import record_payload, records_payload
from crossfoot.mcp.remittance.settings import RemittanceSettings
from crossfoot.mcp.remittance.tools import RemittanceService

SERVER_NAME: Final = "mcp-remittance"
VERSION: Final = "1.0.0"

#: Every tool this server has, all of them reads. Asserted against the live server in tests.
TOOL_NAMES: Final = (
    "get_remittance_for_payment",
    "get_remittance_lines",
    "export_remittances",
)

READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False
)

INSTRUCTIONS: Final = (
    "Remittance advices: the PDFs payers send to say what a payment is for, read-only. One "
    "advice per payment, and most payments have none. `amount` is the total the advice states; "
    "`detail.lines` are its line items exactly as the payer wrote them -- the reference they "
    "cite and the amount they say it is for, which may differ from the invoice by pence or be "
    "a part payment. Nothing here has been checked against the ledger. `free_text` is the "
    "payer's own paragraph, when there is one, and often holds the reason. `record_date` is "
    "the payment date, a calendar date. A foreign advice's `base_minor` is this server's "
    "conversion at the payment-date rate, shown beside it; the payer stated no rate. `party` "
    "is what entity resolution made of the payer name and is often `unresolved`. An advice in "
    "a layout this server cannot read answers `malformed_source`: it exists and needs a "
    "person. A failed call answers with an `error` object carrying a stable `code` rather "
    "than failing the call, so branch on `code`."
)


def build_server(
    settings: RemittanceSettings | None = None, service: RemittanceService | None = None
) -> MCPServer:
    """The remittance MCP server. `service` is injectable so tests can drive it in-process."""
    resolved = settings or RemittanceSettings.from_env()
    remittance = service or RemittanceService(resolved)
    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=VERSION,
        instructions=INSTRUCTIONS,
    )

    @server.tool(annotations=READ_ONLY)
    def get_remittance_for_payment(payment_ref: str) -> Payload:
        """The remittance advice for one payment, by payment reference, e.g. `PAY-2024-0088`.

        `not_found` means no advice was sent for that payment, which is the usual case.
        """
        return guard(lambda: record_payload(remittance.get_remittance_for_payment(payment_ref)))

    @server.tool(annotations=READ_ONLY)
    def get_remittance_lines(remittance_id: str) -> Payload:
        """The line items of one advice, by id, e.g. `remit:RA-2024-0088`.

        Amounts are minor units of `currency`, as the payer wrote them: a credit note is
        negative, and a line need not equal the invoice it cites.
        """

        def body() -> Payload:
            found = remittance.get_remittance_lines(remittance_id)
            return {
                "remittance_id": found.remittance_id,
                "payment_ref": found.payment_ref,
                "currency": found.currency,
                "total_minor": found.total_minor,
                "lines": [line.model_dump(mode="json", exclude_none=True) for line in found.lines],
                "count": len(found.lines),
            }

        return guard(body)

    @server.tool(annotations=READ_ONLY)
    def export_remittances(after_id: str | None = None, limit: int | None = None) -> Payload:
        """Every advice in id order, for bulk ingestion. Page with `after_id` = `next_after_id`.

        `unparsed` lists advices that exist and could not be read, each with its error; they
        are not in `records`. Keep paging while `has_more` is true -- a page can hold no
        records and still not be the last.
        """

        def body() -> Payload:
            page = remittance.export_remittances(after_id=after_id, limit=limit)
            return records_payload(
                page.records,
                unparsed=[
                    {
                        "remittance_id": item.remittance_id,
                        "payment_ref": item.payment_ref,
                        "error": item.error.payload()["error"],
                    }
                    for item in page.unparsed
                ],
                examined=page.examined,
                next_after_id=page.next_after_id,
                has_more=page.has_more,
            )

        return guard(body)

    return server


def run() -> None:  # pragma: no cover - exercised by the stdio integration test
    """Serve on stdio. What `just mcp-remittance` and `python -m crossfoot.mcp.remittance` run."""
    build_server().run("stdio")

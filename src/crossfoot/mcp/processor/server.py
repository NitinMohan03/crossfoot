"""Registration, serialisation, and nothing else. Rule 1.

Every tool here calls the service, turns the result into a payload, and lets `common.guard`
hand a `ToolError` back in the envelope. There is no logic to test at this layer, which is why
one integration test over real stdio is enough and the tool tests talk to `ProcessorService`
directly.

Every tool is annotated `readOnlyHint=True`, and `TOOL_NAMES` is the allowlist
`tests/test_mcp/processor/test_no_write.py` compares the advertised list against: a tool that
writes cannot be added here without that test failing.
"""

from typing import Final

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from crossfoot.mcp.common.errors import Payload, guard
from crossfoot.mcp.common.results import record_payload, records_payload
from crossfoot.mcp.processor.settings import ProcessorSettings
from crossfoot.mcp.processor.tools import ProcessorService

SERVER_NAME: Final = "mcp-processor"
VERSION: Final = "1.0.0"

#: Every tool this server has, all of them reads. Asserted against the live server in tests.
TOOL_NAMES: Final = (
    "get_settlement",
    "list_settlements",
    "get_batch",
    "export_settlements",
    "export_batches",
)

READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False
)

INSTRUCTIONS: Final = (
    "The payment processor's settlements, read-only. A settlement line is one payment the "
    "processor collected: `amount.minor` is the gross the payer paid, in `amount.currency`; "
    "`detail.fee_minor` and `detail.net_minor` are what the processor kept and passed on, in "
    "that same currency, and always add up to the gross. `amount.base_minor` is the gross in "
    "GBP at the settlement-date rate. `record_date` is the settlement date, a calendar date; "
    "`event_time` is when the payment was created, in UTC. The bank never sees a settlement "
    "line: it sees one `TFR` line per batch, named `PAYCO SETTLE BAT_...`, and `get_batch` "
    "with that batch id returns the lines inside it and `payout_base_minor`, the sterling "
    "that reached the bank. `party` is what entity resolution made of the payer name and may "
    "be `unresolved`. A failed call answers with an `error` object carrying a stable `code` "
    "rather than failing the call, so branch on `code`; `unavailable` with `retry_after` means "
    "the processor is rate limiting and says how many seconds to leave it."
)


def build_server(
    settings: ProcessorSettings | None = None, service: ProcessorService | None = None
) -> MCPServer:
    """The processor MCP server. `service` is injectable so tests can drive it in process."""
    resolved = settings or ProcessorSettings.from_env()
    processor = service or ProcessorService(resolved)
    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=VERSION,
        instructions=INSTRUCTIONS,
    )

    @server.tool(annotations=READ_ONLY)
    def get_settlement(id: str) -> Payload:
        """One settlement line by id, e.g. `proc:stl_7f3a09c1d2e4`."""
        return guard(lambda: record_payload(processor.get_settlement(id)))

    @server.tool(annotations=READ_ONLY)
    def list_settlements(
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = None,
    ) -> Payload:
        """Settlement lines by settlement date, oldest first.

        Dates are ISO (`2024-03-15`) and inclusive. When `has_more` is true the window holds
        more lines than the page: narrow the window rather than assume the page holds the line
        you want.
        """

        def body() -> Payload:
            found = processor.list_settlements(date_from=date_from, date_to=date_to, limit=limit)
            return records_payload(found.records, has_more=found.has_more)

        return guard(body)

    @server.tool(annotations=READ_ONLY)
    def get_batch(id: str, limit: int | None = None) -> Payload:
        """A payout batch and the settlement lines inside it.

        `id` is the batch id, as the processor writes it (`bat_2024-03-14_02`) or as the bank's
        `TFR` description does (`BAT_2024-03-14_02`). `batch.payout_base_minor` is the GBP
        amount of that bank line and `batch.paid_out_on` its value date. `total_lines` counts
        every line in the batch; `count` is how many are on this page.
        """

        def body() -> Payload:
            found = processor.get_batch(id, limit=limit)
            return records_payload(
                found.records,
                batch=found.batch.model_dump(mode="json"),
                total_lines=found.total_lines,
            )

        return guard(body)

    @server.tool(annotations=READ_ONLY)
    def export_settlements(cursor: str | None = None, limit: int | None = None) -> Payload:
        """Every settlement line, for bulk ingestion. Page with `cursor` = last `next_cursor`."""

        def body() -> Payload:
            page = processor.export_settlements(cursor=cursor, limit=limit)
            return records_payload(
                page.records, next_cursor=page.next_cursor, has_more=page.has_more
            )

        return guard(body)

    @server.tool(annotations=READ_ONLY)
    def export_batches(cursor: str | None = None, limit: int | None = None) -> Payload:
        """Every payout batch, for bulk ingestion. Page with `cursor` = last `next_cursor`."""

        def body() -> Payload:
            page = processor.export_batches(cursor=cursor, limit=limit)
            return {
                "batches": [batch.model_dump(mode="json") for batch in page.batches],
                "count": len(page.batches),
                "next_cursor": page.next_cursor,
                "has_more": page.has_more,
            }

        return guard(body)

    return server


def run() -> None:  # pragma: no cover - exercised by the stdio integration test
    """Serve on stdio. What `just mcp-processor` and `python -m crossfoot.mcp.processor` run."""
    build_server().run("stdio")

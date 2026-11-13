"""Registration, serialisation, and nothing else. Rule 1.

Every tool here is three lines: call the service, turn the result into a payload, and let
`common.guard` hand a `ToolError` back in the envelope. There is no logic to test at this layer,
which is why one integration test over real stdio is enough and the tool tests talk to
`BankService` directly.

Every tool is annotated `readOnlyHint=True`, and `TOOL_NAMES` is the allowlist
`tests/test_mcp/bank/test_no_write.py` compares the advertised list against: a tool that writes
cannot be added here without that test failing.
"""

from typing import Final

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from crossfoot.mcp.bank.settings import BankSettings
from crossfoot.mcp.bank.tools import BankService
from crossfoot.mcp.common.errors import Payload, guard
from crossfoot.mcp.common.results import record_payload, records_payload

SERVER_NAME: Final = "mcp-bank"
VERSION: Final = "1.0.0"

#: Every tool this server has, all of them reads. Asserted against the live server in tests.
TOOL_NAMES: Final = ("get_bank_line", "find_bank_lines", "export_bank_lines")

READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False
)

INSTRUCTIONS: Final = (
    "The bank statement: one sterling current account, read-only. Amounts are signed GBP minor "
    "units -- money in is positive, a refund negative. `record_date` is the value date, a "
    "calendar date; `event_time` is the posting moment in UTC, with the bank's own local text "
    "beside it. `party` is what entity resolution made of the description, and it is often "
    "`unresolved`: a payer the name alone does not identify is left that way on purpose. "
    "A failed call answers with an `error` object carrying a stable `code` rather than failing "
    "the call, so branch on `code`."
)


def build_server(
    settings: BankSettings | None = None, service: BankService | None = None
) -> MCPServer:
    """The bank MCP server. `service` is injectable so tests can drive it without a transport."""
    resolved = settings or BankSettings.from_env()
    bank = service or BankService(resolved)
    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=VERSION,
        instructions=INSTRUCTIONS,
    )

    @server.tool(annotations=READ_ONLY)
    def get_bank_line(id: str) -> Payload:
        """One statement line by id, e.g. `bank:2024-03-15:000004`."""
        return guard(lambda: record_payload(bank.get_bank_line(id)))

    @server.tool(annotations=READ_ONLY)
    def find_bank_lines(
        amount_min_minor: int | None = None,
        amount_max_minor: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = None,
    ) -> Payload:
        """Statement lines in an amount (signed GBP minor units) and/or value-date window.

        Dates are ISO (`2024-03-15`) and inclusive. `total_matches` counts every line in the
        window; when it is larger than `count`, narrow the window rather than assume the page
        holds the line you want.
        """

        def body() -> Payload:
            records, total = bank.find_bank_lines(
                amount_min_minor=amount_min_minor,
                amount_max_minor=amount_max_minor,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
            return records_payload(records, total_matches=total)

        return guard(body)

    @server.tool(annotations=READ_ONLY)
    def export_bank_lines(after_id: str | None = None, limit: int | None = None) -> Payload:
        """Every line in statement order, for bulk ingestion. Page with `after_id` = last id."""

        def body() -> Payload:
            records = bank.export_bank_lines(after_id=after_id, limit=limit)
            last = records[-1].record_id if records else None
            return records_payload(records, next_after_id=last)

        return guard(body)

    return server


def run() -> None:  # pragma: no cover - exercised by the stdio integration test
    """Serve on stdio. What `just mcp-bank` and `python -m crossfoot.mcp.bank` run."""
    build_server().run("stdio")

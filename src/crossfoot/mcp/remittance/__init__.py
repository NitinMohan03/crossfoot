"""`mcp-remittance`: the MCP server over the remittance advice PDFs (spec 01 §3.4, §6).

The fourth application of the seven rules in `crossfoot.mcp`, and the second over files. The
layout is `mcp/bank/`'s, with the reading split in two because a PDF needs it:

    settings.py    frozen dataclass + from_env(), module-level defaults
    files.py       source access: the package's one file handle, opened read-only
    layout.py      PDF bytes -> text runs with positions; the only user of the PDF library
    parse.py       text runs -> one advice, structurally and strictly
    fx.py          the dataset's rate table, for the sterling view of a foreign advice
    canonical.py   an advice -> CanonicalRecord
    tools.py       the agent-facing semantics, importable without MCP
    server.py      registration and serialisation only
    __main__.py    stdio entry point

**Parsed structurally, not cut into passages.** A line item means nothing without the header
naming the payer (plan §3.5), so the unit read and the unit returned is the whole advice.

**Developed against three of the four layouts.** The fourth is held out (spec 01 §6): nothing
in this package was written for it, nothing here reads `remit.jsonl` or a layout id, and
`tests/test_mcp/remittance/test_held_out.py` reports what the parser makes of it.

The counterparty names its payers are resolved against come from the ERP, read-only, through
`mcp/common/parties.py`, which the bank and processor servers share.

Agent-facing tools (plan §3.3): `get_remittance_for_payment`, `get_remittance_lines`, plus
`export_remittances`, the bulk read ingestion uses.
"""

from crossfoot.mcp.remittance.server import TOOL_NAMES, build_server, run
from crossfoot.mcp.remittance.settings import RemittanceSettings
from crossfoot.mcp.remittance.tools import RemittanceService

__all__ = ["TOOL_NAMES", "RemittanceService", "RemittanceSettings", "build_server", "run"]

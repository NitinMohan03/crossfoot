"""`mcp-bank`: the MCP server over the UK bank statement CSV export (spec 01 §3.2).

The second application of the seven rules in `crossfoot.mcp`, and the first over a source that
is files. The layout is `mcp/erp/`'s, slot for slot:

    settings.py    frozen dataclass + from_env(), module-level defaults
    files.py       source access: the package's one file handle, opened read-only
    parse.py       the export's text -> rows, strict
    canonical.py   this source's rows -> CanonicalRecord
    tools.py       the agent-facing semantics, importable without MCP
    server.py      registration and serialisation only
    __main__.py    stdio entry point

The counterparty names its descriptions are resolved against come from the ERP, read-only,
through `mcp/common/parties.py`, which `mcp-processor` shares.

Agent-facing tools (plan §3.3): `get_bank_line`, `find_bank_lines`, plus `export_bank_lines`,
the bulk read ingestion uses.
"""

from crossfoot.mcp.bank.server import TOOL_NAMES, build_server, run
from crossfoot.mcp.bank.settings import BankSettings
from crossfoot.mcp.bank.tools import BankService

__all__ = ["TOOL_NAMES", "BankService", "BankSettings", "build_server", "run"]

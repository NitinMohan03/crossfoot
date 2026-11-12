"""`mcp-erp`: the ERP (Postgres) MCP server, and the pattern the other three follow.

Read `crossfoot.mcp` for the seven rules; this package is their first application. The layout a
later server copies:

    settings.py    frozen dataclass + from_env(), module-level defaults
    db.py          source access, read-only enforced at the source itself
    queries.py     the SQL, every value a parameter
    canonical.py   this source's rows -> CanonicalRecord
    tools.py       the agent-facing semantics, importable without MCP
    server.py      registration and serialisation only
    __main__.py    stdio entry point

Agent-facing tools (plan §3.3): `get_invoice`, `list_open_invoices`, `get_credit_notes`,
`lookup_alias` -- the shared registry tool, hosted here because the registry lives in this
database -- plus `export_invoices`, the bulk read ingestion uses.
"""

from crossfoot.mcp.erp.server import TOOL_NAMES, build_server, run
from crossfoot.mcp.erp.settings import ErpSettings
from crossfoot.mcp.erp.tools import ErpService

__all__ = ["TOOL_NAMES", "ErpService", "ErpSettings", "build_server", "run"]

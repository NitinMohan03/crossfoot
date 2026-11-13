"""`mcp-processor`: the MCP server over the payment processor's REST API (spec 01 §3.3).

The third application of the seven rules in `crossfoot.mcp`, and the first over a source that
is a network service. The layout is `mcp/erp/`'s, slot for slot:

    settings.py    frozen dataclass + from_env(), module-level defaults; where the token lives
    http.py        source access: the package's one request, and it is a GET
    client.py      the bearer token, the URLs, the bounded 429, and what each status means
    wire.py        the API's JSON -> strict models, each record keeping its own text
    canonical.py   settlement lines -> CanonicalRecord
    tools.py       the agent-facing semantics, importable without MCP
    server.py      registration and serialisation only
    __main__.py    stdio entry point

The counterparty names its payers are resolved against come from the ERP, read-only, through
`mcp/common/parties.py`, which `mcp-bank` shares.

Agent-facing tools (plan §3.3): `get_settlement`, `list_settlements`, and `get_batch`, which
opens the one bank line a batch becomes; plus `export_settlements` and `export_batches`, the
bulk reads ingestion uses.
"""

from crossfoot.mcp.processor.server import TOOL_NAMES, build_server, run
from crossfoot.mcp.processor.settings import ProcessorSettings
from crossfoot.mcp.processor.tools import ProcessorService

__all__ = ["TOOL_NAMES", "ProcessorService", "ProcessorSettings", "build_server", "run"]

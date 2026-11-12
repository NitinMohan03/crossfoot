"""`python -m crossfoot.mcp.erp` -- what `just mcp-erp` runs. Transport is stdio (plan §3.3)."""

import argparse

from crossfoot.mcp.erp.server import build_server
from crossfoot.mcp.erp.settings import ErpSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="The crossfoot ERP MCP server (stdio).")
    parser.add_argument("--dsn", help="read-only Postgres DSN; defaults to the crossfoot_ro role")
    parser.add_argument("--schema", help="ERP schema to read; defaults to `erp`")
    args = parser.parse_args()
    settings = ErpSettings.from_env(dsn=args.dsn, schema=args.schema)
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()

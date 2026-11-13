"""`python -m crossfoot.mcp.processor` -- what `just mcp-processor` runs. Transport is stdio.

The bearer token has no flag here on purpose: it is read from `CROSSFOOT_PROCESSOR_TOKEN`, and a
command line is visible to anything that can list processes.
"""

import argparse

from crossfoot.mcp.processor.server import build_server
from crossfoot.mcp.processor.settings import ProcessorSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="The crossfoot processor MCP server (stdio).")
    parser.add_argument(
        "--url", help="base URL of the processor API; defaults to http://127.0.0.1:8099"
    )
    parser.add_argument("--dsn", help="read-only Postgres DSN; defaults to the crossfoot_ro role")
    parser.add_argument(
        "--erp-schema", help="schema holding the counterparties to resolve against; default `erp`"
    )
    args = parser.parse_args()
    settings = ProcessorSettings.from_env(
        base_url=args.url, dsn=args.dsn, erp_schema=args.erp_schema
    )
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()

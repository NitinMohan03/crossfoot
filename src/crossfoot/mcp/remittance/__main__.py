"""`python -m crossfoot.mcp.remittance` -- what `just mcp-remittance` runs. Transport is stdio."""

import argparse

from crossfoot.mcp.remittance.server import build_server
from crossfoot.mcp.remittance.settings import RemittanceSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="The crossfoot remittance MCP server (stdio).")
    parser.add_argument("--data", help="dataset directory holding `remit/`; defaults to data/full")
    parser.add_argument("--dsn", help="read-only Postgres DSN; defaults to the crossfoot_ro role")
    parser.add_argument(
        "--erp-schema", help="schema holding the counterparties to resolve against; default `erp`"
    )
    args = parser.parse_args()
    settings = RemittanceSettings.from_env(
        data_dir=args.data, dsn=args.dsn, erp_schema=args.erp_schema
    )
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()

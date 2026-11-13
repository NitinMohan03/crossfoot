"""`python -m crossfoot.mcp.bank` -- what `just mcp-bank` runs. Transport is stdio (plan §3.3)."""

import argparse

from crossfoot.mcp.bank.server import build_server
from crossfoot.mcp.bank.settings import BankSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="The crossfoot bank MCP server (stdio).")
    parser.add_argument("--data", help="dataset directory holding `bank/`; defaults to data/full")
    parser.add_argument("--dsn", help="read-only Postgres DSN; defaults to the crossfoot_ro role")
    parser.add_argument(
        "--erp-schema", help="schema holding the counterparties to resolve against; default `erp`"
    )
    args = parser.parse_args()
    settings = BankSettings.from_env(data_dir=args.data, dsn=args.dsn, erp_schema=args.erp_schema)
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()

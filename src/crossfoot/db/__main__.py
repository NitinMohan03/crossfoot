"""The `crossfoot.db` CLI: what `just db-ref` and `just db-er` run."""

import argparse
from pathlib import Path

from crossfoot.db.connect import RO_USER, connection
from crossfoot.db.reference import load_reference
from crossfoot.db.roles import apply_roles
from crossfoot.db.schema import apply_er


def main() -> None:
    parser = argparse.ArgumentParser(description="Load reference data into Postgres.")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load-reference", help="load the FX rate and bank-holiday tables")
    load.add_argument("--dir", type=Path, default=Path("data/full"), help="dataset directory")
    sub.add_parser("apply-er", help="create the `er` schema and the alias registry table")
    sub.add_parser("apply-roles", help="create the read-only role and its SELECT grants")
    args = parser.parse_args()

    if args.command == "apply-er":
        with connection() as conn:
            apply_er(conn)
        print("applied er.sql: er.counterparty_alias is present")
        return

    if args.command == "apply-roles":
        with connection() as conn:
            granted = apply_roles(conn)
        print(f"{RO_USER} may SELECT in: {', '.join(granted) or '(no schemas present yet)'}")
        return

    with connection() as conn:
        counts = load_reference(args.dir, conn)
    print(
        f"loaded ref.fx_rates: {counts.fx_rates} rows, "
        f"ref.bank_holidays: {counts.bank_holidays} rows (from {args.dir})"
    )


if __name__ == "__main__":
    main()

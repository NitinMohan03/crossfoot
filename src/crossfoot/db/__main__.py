"""`python -m crossfoot.db load-reference --dir data/full` -- what `just db-ref` runs."""

import argparse
from pathlib import Path

from crossfoot.db.connect import connection
from crossfoot.db.reference import load_reference


def main() -> None:
    parser = argparse.ArgumentParser(description="Load reference data into Postgres.")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load-reference", help="load the FX rate and bank-holiday tables")
    load.add_argument("--dir", type=Path, default=Path("data/full"), help="dataset directory")
    args = parser.parse_args()

    with connection() as conn:
        counts = load_reference(args.dir, conn)
    print(
        f"loaded ref.fx_rates: {counts.fx_rates} rows, "
        f"ref.bank_holidays: {counts.bank_holidays} rows (from {args.dir})"
    )


if __name__ == "__main__":
    main()

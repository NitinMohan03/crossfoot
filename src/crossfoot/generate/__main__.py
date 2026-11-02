"""`python -m crossfoot.generate --seed 42 --kind full --out data/full`."""

import argparse
import sys
from pathlib import Path

from crossfoot.generate.dataset import FULL_EVENTS, SLICE_EVENTS, generate, write
from crossfoot.generate.guards import GuardError, require_golden_opt_in, require_test_opt_in
from crossfoot.generate.seeds import FULL_SEED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crossfoot.generate")
    parser.add_argument("--seed", type=int, default=FULL_SEED)
    parser.add_argument("--kind", choices=("full", "slice"), default="full")
    parser.add_argument("--events", type=int, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        require_test_opt_in(args.seed)
        require_golden_opt_in(args.out)
    except GuardError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    count = args.events or (FULL_EVENTS if args.kind == "full" else SLICE_EVENTS)
    dataset = generate(args.seed, args.kind, count)
    write(args.out, dataset)

    counts = ", ".join(f"{name}={value}" for name, value in sorted(dataset.type_counts.items()))
    print(f"seed={args.seed} kind={args.kind} events={len(dataset.events)} -> {args.out}")
    print(f"  types: {counts}")
    print(
        f"  records: {sum(len(event.invoices) for event in dataset.events)} invoices, "
        f"{len(dataset.bank_plan.all_rows())} bank lines, "
        f"{sum(batch.count for batch in dataset.processor_plan.batches)} settlements in "
        f"{len(dataset.processor_plan.batches)} batches"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

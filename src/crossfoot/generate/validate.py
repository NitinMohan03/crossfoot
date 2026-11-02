"""Ground-truth validator (spec 01 §10).

`python -m crossfoot.generate.validate data/full` re-reads a dataset from disk and asserts the
invariants the rest of the project is allowed to assume. Checks that need types Session 1.3
has not built yet are listed as pending rather than silently passing.
"""

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final

from crossfoot.config import Thresholds
from crossfoot.generate.calendars import (
    add_business_days,
    business_days_between,
    spans_non_business_day,
)
from crossfoot.generate.read import (
    BankLine,
    Erp,
    read_bank,
    read_erp,
    read_jsonl,
    read_manifest,
    read_text_raw,
)
from crossfoot.money import ratio
from crossfoot.names import trivially_normalises

DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BANK_DATE_RE: Final = re.compile(r"^\d{2}/\d{2}/\d{4}$")
AMOUNT_RE: Final = re.compile(r"^-?\d+\.\d{2}$")
DECOY_TYPES: Final = {"clean", "timing", "name"}
TIMING_LAG: Final = (2, 5)

PENDING: Final = (
    "partial/consolidated exact sums (spec 10.3)",
    "fx reproduction from the rate table (spec 10.3)",
    "reasoning short-pay and rounding residuals (spec 10.3)",
    "name-variant similarity floor (spec 10.5)",
    "reasoning(a) similarity ceiling (spec 10.5)",
    "per-type PDF presence within 3pp (spec 10.6)",
    "duplicate perturbation (spec 10.2)",
)


@dataclass(slots=True)
class Result:
    name: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


@dataclass(slots=True)
class Dataset:
    path: Path
    header: dict[str, Any]
    groups: list[dict[str, Any]]
    erp: Erp
    bank: list[BankLine]
    settlements: list[dict[str, Any]]
    batches: list[dict[str, Any]]
    posted_at: list[dict[str, Any]]

    def bank_by_id(self) -> dict[str, BankLine]:
        return {line.id: line for line in self.bank}

    def settlement_by_id(self) -> dict[str, dict[str, Any]]:
        return {row["id"]: row for row in self.settlements}

    def events(self) -> list[dict[str, Any]]:
        return [group for group in self.groups if group.get("event_id")]


def load(path: Path) -> Dataset:
    header, groups = read_manifest(path / "manifest.jsonl")
    return Dataset(
        path=path,
        header=header,
        groups=groups,
        erp=read_erp(path / "erp.sql"),
        bank=read_bank(path / "bank"),
        settlements=read_jsonl(path / "processor" / "settlements.jsonl"),
        batches=read_jsonl(path / "processor" / "batches.jsonl"),
        posted_at=read_jsonl(path / "bank" / "posted_at.jsonl"),
    )


def check_counts(data: Dataset) -> Result:
    result = Result("1. counts")
    observed = Counter(str(group["match_type"]) for group in data.events())
    declared = {str(name): int(count) for name, count in data.header["type_counts"].items()}
    for name, count in declared.items():
        if observed.get(name, 0) != count:
            result.fail(f"{name}: header says {count}, manifest holds {observed.get(name, 0)}")
    for name, count in observed.items():
        if name not in declared:
            result.fail(f"{name}: {count} groups but the header does not declare the type")
        result.note(f"{name}={count}")

    kinds = Counter(
        str(group.get("kind"))
        for group in data.groups
        if group["match_type"] == "no_match" and group.get("kind")
    )
    declared_kinds = {str(k): int(v) for k, v in data.header["no_match_kinds"].items()}
    if dict(kinds) != declared_kinds:
        result.fail(f"no_match kinds: header {declared_kinds} vs manifest {dict(kinds)}")
    for name, count in sorted(kinds.items()):
        result.note(f"no_match/{name}={count}")

    total = sum(declared.values())
    if total != int(data.header["event_count"]):
        result.fail(f"event_count {data.header['event_count']} != sum of type_counts {total}")
    if data.header["kind"] == "slice":
        for name, count in declared.items():
            if count < 100:
                result.fail(f"slice floor: {name}={count} < 100")
    return result


def check_referential(data: Dataset) -> Result:
    result = Result("2. referential integrity")
    source_ids = (
        {invoice.record_id for invoice in data.erp.invoices}
        | {note.record_id for note in data.erp.credit_notes}
        | {line.id for line in data.bank}
        | {str(row["id"]) for row in data.settlements}
    )
    seen: Counter[str] = Counter()
    for group in data.groups:
        for key in ("source_ids", "target_ids", "evidence_ids"):
            seen.update(str(value) for value in group.get(key, []))

    for record_id, count in seen.items():
        if count > 1:
            result.fail(f"{record_id} appears in {count} groups")
    missing_from_manifest = sorted(source_ids - set(seen))
    if missing_from_manifest:
        result.fail(
            f"{len(missing_from_manifest)} source records in no group, "
            f"e.g. {missing_from_manifest[:3]}"
        )
    missing_from_sources = sorted(set(seen) - source_ids)
    if missing_from_sources:
        result.fail(
            f"{len(missing_from_sources)} manifest ids with no source record, "
            f"e.g. {missing_from_sources[:3]}"
        )

    posted_ids = {str(row["id"]) for row in data.posted_at}
    bank_ids = {line.id for line in data.bank}
    if posted_ids != bank_ids:
        result.fail(
            f"posted_at.jsonl and the CSVs disagree on {len(posted_ids ^ bank_ids)} bank ids"
        )

    by_id = {str(group.get("duplicate_of")) for group in data.groups if group.get("duplicate_of")}
    duplicates = {
        str(group["target_ids"][0])
        for group in data.groups
        if group["match_type"] == "duplicate" and group["target_ids"]
    }
    for canonical in sorted(by_id):
        if canonical not in source_ids:
            result.fail(f"duplicate_of {canonical} does not exist")
        if canonical in duplicates:
            result.fail(f"duplicate_of {canonical} is itself a duplicate")
    result.note(f"{len(source_ids)} source records across {len(data.groups)} groups")
    return result


def check_arithmetic(data: Dataset) -> Result:
    result = Result("3. arithmetic")
    low, high = Thresholds.FEE_WINDOW
    for row in data.settlements:
        gross, fee, net = int(row["gross_minor"]), int(row["fee_minor"]), int(row["net_minor"])
        if gross - fee != net:
            result.fail(f"{row['id']}: gross-fee={gross - fee} but net={net}")
        if (row["product"] == "card") != (fee > 0):
            result.fail(f"{row['id']}: product={row['product']} with fee_minor={fee}")
        if fee > 0 and not low < ratio(net, gross) < high:
            result.fail(f"{row['id']}: net/gross {ratio(net, gross)} outside FEE_WINDOW")

    by_batch: dict[str, list[dict[str, Any]]] = {}
    for row in data.settlements:
        by_batch.setdefault(str(row["batch_id"]), []).append(row)
    batch_by_id = {str(batch["id"]): batch for batch in data.batches}
    for batch_id, rows in sorted(by_batch.items()):
        batch = batch_by_id.get(batch_id)
        if batch is None:
            result.fail(f"{batch_id}: settlements reference a batch missing from batches.jsonl")
            continue
        total = sum(int(row["net_minor"]) for row in rows)
        if total != int(batch["total_net_minor"]):
            result.fail(f"{batch_id}: total_net_minor {batch['total_net_minor']} != {total}")
        if len(rows) != int(batch["count"]):
            result.fail(f"{batch_id}: count {batch['count']} != {len(rows)} lines")

    payout_by_batch = {
        str(group["notes"]).removeprefix("batch="): group
        for group in data.groups
        if group.get("kind") == "batch_payout" and group.get("notes")
    }
    bank_by_id = data.bank_by_id()
    for batch_id, batch in sorted(batch_by_id.items()):
        group = payout_by_batch.get(batch_id)
        if group is None:
            result.fail(f"{batch_id}: no batch_payout bank line in the manifest")
            continue
        line = bank_by_id.get(str(group["target_ids"][0]))
        if line is None:
            result.fail(f"{batch_id}: payout line {group['target_ids']} missing from the CSVs")
            continue
        if line.paid_in_minor != int(batch["total_net_minor"]):
            result.fail(
                f"{batch_id}: payout credit {line.paid_in_minor} != "
                f"total_net_minor {batch['total_net_minor']}"
            )
        if line.value_date.isoformat() != batch["paid_out_on"]:
            result.fail(f"{batch_id}: payout dated {line.value_date}, not {batch['paid_out_on']}")
        if line.txn_type != "TFR":
            result.fail(f"{batch_id}: payout line type {line.txn_type} is not TFR")
    result.note(f"{len(data.settlements)} settlements in {len(data.batches)} batches")
    return result


def _target_date(data: Dataset, record_id: str) -> date | None:
    if record_id.startswith("bank:"):
        line = data.bank_by_id().get(record_id)
        return None if line is None else line.value_date
    row = data.settlement_by_id().get(record_id)
    return None if row is None else date.fromisoformat(str(row["settled_on"]))


def check_dates(data: Dataset) -> Result:
    result = Result("4. dates")
    invoices = data.erp.invoice_by_ref()
    spanning = 0
    timing_total = 0
    for group in data.events():
        match_type = str(group["match_type"])
        if match_type not in {"clean", "fee", "timing"}:
            continue
        refs = [str(value).removeprefix("erp:") for value in group["source_ids"]]
        invoice = invoices.get(refs[0]) if refs else None
        if invoice is None:
            result.fail(f"{group['event_id']}: source invoice {refs} not in erp.sql")
            continue
        for record_id in group["target_ids"]:
            value_date = _target_date(data, str(record_id))
            if value_date is None:
                result.fail(f"{group['event_id']}: target {record_id} has no date")
                continue
            if match_type in {"clean", "fee"}:
                limit = add_business_days(invoice.due_date, 1)
                if value_date > limit:
                    result.fail(
                        f"{group['event_id']}: {match_type} paid {value_date} after due+1bd {limit}"
                    )
            else:
                timing_total += 1
                lag = business_days_between(invoice.due_date, value_date)
                if not TIMING_LAG[0] <= lag <= TIMING_LAG[1]:
                    result.fail(f"{group['event_id']}: timing lag {lag}bd outside {TIMING_LAG}")
                if spans_non_business_day(invoice.due_date, value_date):
                    spanning += 1
    if timing_total:
        share = spanning * 100 // timing_total
        result.note(f"timing spanning a non-business day: {spanning}/{timing_total} ({share}%)")
        if share < 50:
            result.fail(f"only {share}% of timing cases span a non-business day, need >=50%")
    return result


def check_names(data: Dataset) -> Result:
    result = Result("5. names")
    invoices = data.erp.invoice_by_ref()
    counterparties = data.erp.counterparty_by_id()
    bank_by_id = data.bank_by_id()
    settlements = data.settlement_by_id()
    for group in data.events():
        if str(group["match_type"]) not in {"clean", "fee", "timing"}:
            continue
        refs = [str(value).removeprefix("erp:") for value in group["source_ids"]]
        invoice = invoices.get(refs[0]) if refs else None
        if invoice is None:
            continue
        legal = counterparties[invoice.counterparty_id].legal_name
        for record_id in group["target_ids"]:
            key = str(record_id)
            if key.startswith("bank:"):
                line = bank_by_id.get(key)
                if line is None:
                    continue
                tokens = line.description.split(" ")
                if not any(
                    trivially_normalises(legal, " ".join(tokens[:size]))
                    for size in range(len(tokens), 0, -1)
                ):
                    result.fail(f"{key}: {line.description!r} does not normalise to {legal!r}")
            else:
                row = settlements.get(key)
                if row is not None and not trivially_normalises(legal, str(row["payer_name"])):
                    result.fail(f"{key}: {row['payer_name']!r} does not normalise to {legal!r}")
    return result


def check_leakage(data: Dataset) -> Result:
    result = Result("6. leakage")
    for group in data.events():
        decoys = group.get("decoys") or []
        if decoys and str(group["match_type"]) not in DECOY_TYPES:
            result.fail(f"{group['event_id']}: {group['match_type']} carries decoys {decoys}")
    tagged = sum(1 for group in data.events() if group.get("decoys"))
    result.note(f"{tagged} events carry a decoy tag")
    return result


def check_money_and_dates(data: Dataset) -> Result:
    result = Result("7. money and dates")
    package = Path(__file__).parent
    needle = "float" + "("  # assembled so this check can scan its own source too
    for source in sorted(package.rglob("*.py")):
        if needle in source.read_text(encoding="utf-8"):
            result.fail(
                f"{source.relative_to(package)}: calls the float constructor; "
                "money is Decimal or integer minor units"
            )

    for row in data.settlements:
        for key in ("gross_minor", "fee_minor", "net_minor"):
            if not isinstance(row[key], int):
                result.fail(f"{row['id']}: {key} is {type(row[key]).__name__}, not int")
        if not DATE_RE.match(str(row["settled_on"])):
            result.fail(f"{row['id']}: settled_on {row['settled_on']} is not a calendar date")

    for csv_path in sorted((data.path / "bank").glob("*.csv")):
        for csv_row in read_text_raw(csv_path).split("\r\n")[1:]:
            if not csv_row:
                continue
            fields = csv_row.split(",")
            if not BANK_DATE_RE.match(fields[0]):
                result.fail(f"{csv_path.name}: {fields[0]!r} is not DD/MM/YYYY")
            for amount in (fields[3], fields[4], fields[5]):
                if amount and not AMOUNT_RE.match(amount):
                    result.fail(f"{csv_path.name}: {amount!r} is not a 2dp amount")

    for group in data.events():
        if not DATE_RE.match(str(group["paid_on"])):
            result.fail(f"{group['event_id']}: paid_on {group['paid_on']} is not a date")
    return result


CHECKS = (
    check_counts,
    check_referential,
    check_arithmetic,
    check_dates,
    check_names,
    check_leakage,
    check_money_and_dates,
)


def run(path: Path) -> list[Result]:
    data = load(path)
    return [check(data) for check in CHECKS]


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    path = Path(args[0]) if args else Path("data/full")
    if not (path / "manifest.jsonl").exists():
        print(f"error: {path}/manifest.jsonl not found", file=sys.stderr)
        return 2

    results = run(path)
    width = max(len(result.name) for result in results)
    for result in results:
        status = "ok" if result.ok else f"FAIL ({len(result.failures)})"
        print(f"{result.name.ljust(width)}  {status}")
        for note in result.notes:
            print(f"    - {note}")
        for failure in result.failures[:10]:
            print(f"    ! {failure}")
        if len(result.failures) > 10:
            print(f"    ! ... {len(result.failures) - 10} more")

    print("\npending until Session 1.3:")
    for item in PENDING:
        print(f"    . {item}")

    passed = sum(1 for result in results if result.ok)
    print(f"\n{passed}/{len(results)} checks ok")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

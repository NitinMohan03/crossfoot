"""Ground-truth validator (spec 01 §10).

`python -m crossfoot.generate.validate data/full` re-reads a dataset from disk and asserts the
invariants the rest of the project is allowed to assume. It re-parses `erp.sql`, the bank CSVs
and `fx_rates.csv` rather than trusting an in-memory copy, so a writer bug cannot hide behind
itself.

Session 1.3 completed every check in §10; `PENDING` is empty and stays empty.
"""

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from crossfoot.config import HELD_OUT_TEMPLATE, Thresholds
from crossfoot.generate.calendars import (
    add_business_days,
    business_days_between,
    spans_non_business_day,
)
from crossfoot.generate.fx import to_base_minor
from crossfoot.generate.read import (
    BankLine,
    Erp,
    pdf_names,
    read_bank,
    read_erp,
    read_fx,
    read_jsonl,
    read_manifest,
    read_remit,
    read_text_raw,
)
from crossfoot.generate.remittance import (
    GLOBAL_TARGET_PCT,
    MANDATORY_SUBTYPES,
    MANDATORY_TYPES,
    NEVER_TYPES,
    TOPPABLE_SUBTYPES,
    TOPPABLE_TYPES,
)
from crossfoot.money import ratio
from crossfoot.names import normalises_to_any, surface_similarity

DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BANK_DATE_RE: Final = re.compile(r"^\d{2}/\d{2}/\d{4}$")
AMOUNT_RE: Final = re.compile(r"^-?\d+\.\d{2}$")
DECOY_TYPES: Final = {"clean", "timing", "name"}
DECOY_TAGS: Final = {"amount_collision", "near_amount", "recurring", "round_amount"}
TIMING_LAG: Final = (2, 5)
ROUNDING_RESIDUAL: Final = (1, 5)
PARTIAL_GAP_DAYS: Final = (1, 45)
# §10.6: each non-mandatory type's PDF rate, against the pooled non-mandatory rate.
PDF_RATE_TOLERANCE_PP: Final = 3
# A bucket of n events can only express rates in steps of 100/n pp, so a tolerance finer than
# that step is unsatisfiable however the top-up is assigned. Both real datasets clear this;
# small test worlds do not, and the check says so rather than failing on arithmetic.
MIN_BUCKET_FOR_RATE_CHECK: Final = 100 // PDF_RATE_TOLERANCE_PP + 1
# Types whose payment must land inside clean's window (spec 01 §4.1 and the classes that
# inherit it: the discrepancy is in the name, the currency or the amount, never the date).
ON_TIME_TYPES: Final = {"clean", "fee", "name", "fx", "duplicate"}

PENDING: Final[tuple[str, ...]] = ()


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
    remit: list[dict[str, Any]]
    pdfs: set[str]
    fx: dict[date, dict[str, int]]

    def bank_by_id(self) -> dict[str, BankLine]:
        return {line.id: line for line in self.bank}

    def settlement_by_id(self) -> dict[str, dict[str, Any]]:
        return {row["id"]: row for row in self.settlements}

    def remit_by_id(self) -> dict[str, dict[str, Any]]:
        return {str(row["id"]): row for row in self.remit}

    def events(self) -> list[dict[str, Any]]:
        return [group for group in self.groups if group.get("event_id")]

    def invoices_of(self, group: dict[str, Any]) -> list[Any]:
        """The ERP invoices on a group's source side, in ref order."""
        by_ref = self.erp.invoice_by_ref()
        refs = [str(value).removeprefix("erp:") for value in group["source_ids"]]
        return [by_ref[ref] for ref in refs if ref in by_ref]

    def credit_notes_of(self, group: dict[str, Any]) -> list[Any]:
        by_ref = {note.ref: note for note in self.erp.credit_notes}
        refs = [str(value).removeprefix("erp:") for value in group["source_ids"]]
        return [by_ref[ref] for ref in refs if ref in by_ref]

    def target_amount(self, record_id: str) -> int | None:
        """What the payment record says was paid, in its own currency."""
        if record_id.startswith("bank:"):
            line = self.bank_by_id().get(record_id)
            return None if line is None else line.paid_in_minor
        row = self.settlement_by_id().get(record_id)
        return None if row is None else int(row["gross_minor"])

    def target_date(self, record_id: str) -> date | None:
        if record_id.startswith("bank:"):
            line = self.bank_by_id().get(record_id)
            return None if line is None else line.value_date
        row = self.settlement_by_id().get(record_id)
        return None if row is None else date.fromisoformat(str(row["settled_on"]))

    def payer_surface(self, record_id: str) -> str | None:
        """The payer name as the matcher will read it off this record."""
        if record_id.startswith("bank:"):
            line = self.bank_by_id().get(record_id)
            return None if line is None else line.description
        row = self.settlement_by_id().get(record_id)
        return None if row is None else str(row["payer_name"])


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
        remit=read_remit(path / "remit"),
        pdfs=pdf_names(path / "remit"),
        fx=read_fx(path / "fx_rates.csv"),
    )


def subtype_of(group: dict[str, Any]) -> str | None:
    value = group.get("reasoning_subtype")
    return None if value is None else str(value)


def _label(group: dict[str, Any]) -> str:
    """The per-type table's row key: the match type, or `reasoning/<sub-type>`."""
    match_type = str(group["match_type"])
    subtype = subtype_of(group)
    return f"{match_type}/{subtype}" if subtype else match_type


# --------------------------------------------------------------------------- 1. counts


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

    subtypes = Counter(
        subtype_of(group) for group in data.events() if str(group["match_type"]) == "reasoning"
    )
    if None in subtypes:
        result.fail(f"{subtypes[None]} reasoning groups carry no reasoning_subtype")
    for name, count in sorted((str(k), v) for k, v in subtypes.items() if k is not None):
        result.note(f"reasoning/{name}={count}")
    reasoning_total = observed.get("reasoning", 0)
    counted = sum(count for key, count in subtypes.items() if key is not None)
    if counted != reasoning_total:
        result.fail(f"reasoning sub-types sum to {counted}, not {reasoning_total}")

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

    # Only the two event orphans count toward the 1% quota (spec 01 §4.10).
    orphans = kinds.get("open_invoice", 0) + kinds.get("unknown_receipt", 0)
    if orphans != observed.get("no_match", 0):
        result.fail(f"{orphans} event orphans but {observed.get('no_match', 0)} no_match events")

    # Every duplicate event contributes exactly one copy group (§4.9).
    copies = sum(
        1
        for group in data.groups
        if group["match_type"] == "duplicate" and not group.get("event_id")
    )
    if copies != observed.get("duplicate", 0):
        result.fail(f"{copies} duplicate copy groups for {observed.get('duplicate', 0)} events")
    result.note(f"duplicate copy groups={copies}")

    total = sum(declared.values())
    if total != int(data.header["event_count"]):
        result.fail(f"event_count {data.header['event_count']} != sum of type_counts {total}")
    if data.header["kind"] == "slice":
        for name, count in declared.items():
            if count < 100:
                result.fail(f"slice floor: {name}={count} < 100")
    return result


# ------------------------------------------------------------- 2. referential integrity


def check_referential(data: Dataset) -> Result:
    result = Result("2. referential integrity")
    source_ids = (
        {invoice.record_id for invoice in data.erp.invoices}
        | {note.record_id for note in data.erp.credit_notes}
        | {line.id for line in data.bank}
        | {str(row["id"]) for row in data.settlements}
        | {str(row["id"]) for row in data.remit}
    )
    seen: Counter[str] = Counter()
    for group in data.groups:
        for key in ("source_ids", "target_ids", "evidence_ids"):
            seen.update(str(value) for value in group.get(key, []))

    for record_id, count in sorted(seen.items()):
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

    # One PDF on disk per advice, and no stray PDFs (spec 01 §3.4).
    refs = {str(row["payment_ref"]) for row in data.remit}
    if refs != data.pdfs:
        result.fail(
            f"remit.jsonl and the PDFs on disk disagree on {len(refs ^ data.pdfs)} payment refs"
        )

    duplicates = {
        str(group["target_ids"][0])
        for group in data.groups
        if group["match_type"] == "duplicate" and not group.get("event_id") and group["target_ids"]
    }
    canonicals = {
        str(group.get("duplicate_of")) for group in data.groups if group.get("duplicate_of")
    }
    for canonical in sorted(canonicals):
        if canonical not in source_ids:
            result.fail(f"duplicate_of {canonical} does not exist")
        if canonical in duplicates:
            result.fail(f"duplicate_of {canonical} is itself a duplicate")
    # "The earlier id is canonical" (§4.9).
    for group in data.groups:
        if group["match_type"] != "duplicate" or group.get("event_id"):
            continue
        copy = str(group["target_ids"][0])
        canonical = str(group.get("duplicate_of"))
        if canonical >= copy:
            result.fail(f"{copy}: canonical {canonical} is not the earlier id")

    result.note(f"{len(source_ids)} source records across {len(data.groups)} groups")
    result.note(f"{len(data.remit)} remittance advices, {len(data.pdfs)} PDFs")
    return result


# ----------------------------------------------------------------------- 3. arithmetic


def _check_settlement_arithmetic(data: Dataset, result: Result) -> None:
    low, high = Thresholds.FEE_WINDOW
    for row in data.settlements:
        gross, fee, net = int(row["gross_minor"]), int(row["fee_minor"]), int(row["net_minor"])
        if gross - fee != net:
            result.fail(f"{row['id']}: gross-fee={gross - fee} but net={net}")
        if (row["product"] == "card") != (fee > 0):
            result.fail(f"{row['id']}: product={row['product']} with fee_minor={fee}")
        if fee > 0 and not low < ratio(net, gross) < high:
            result.fail(f"{row['id']}: net/gross {ratio(net, gross)} outside FEE_WINDOW")
        # A foreign settlement records the rate it used; a GBP one has nothing to record.
        foreign = str(row["currency"]) != "GBP"
        if foreign != (row.get("fx_rate") is not None):
            result.fail(
                f"{row['id']}: currency={row['currency']} with fx_rate={row.get('fx_rate')}"
            )


def _check_batches(data: Dataset, result: Result) -> None:
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
        currencies = {str(row["currency"]) for row in rows}
        if currencies != {str(batch["currency"])}:
            result.fail(f"{batch_id}: holds {sorted(currencies)} but declares {batch['currency']}")

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
        currency = str(batch["currency"])
        expected = int(batch["payout_base_minor"])
        if currency == "GBP" and expected != int(batch["total_net_minor"]):
            result.fail(
                f"{batch_id}: GBP payout {expected} != total_net {batch['total_net_minor']}"
            )
        if currency != "GBP":
            paid_out_on = date.fromisoformat(str(batch["paid_out_on"]))
            rate = data.fx.get(paid_out_on, {}).get(currency)
            if rate is None:
                result.fail(f"{batch_id}: no {currency} rate for {paid_out_on}")
            elif to_base_minor(int(batch["total_net_minor"]), rate) != expected:
                result.fail(
                    f"{batch_id}: payout {expected} is not {batch['total_net_minor']} "
                    f"{currency} at the {paid_out_on} rate"
                )
        if line.paid_in_minor != expected:
            result.fail(f"{batch_id}: payout credit {line.paid_in_minor} != {expected}")
        if line.value_date.isoformat() != batch["paid_out_on"]:
            result.fail(f"{batch_id}: payout dated {line.value_date}, not {batch['paid_out_on']}")
        if line.txn_type != "TFR":
            result.fail(f"{batch_id}: payout line type {line.txn_type} is not TFR")


def _check_group_sums(data: Dataset, result: Result) -> None:
    low, high = Thresholds.FEE_WINDOW
    for group in data.events():
        match_type = str(group["match_type"])
        subtype = subtype_of(group)
        invoices = data.invoices_of(group)
        notes = data.credit_notes_of(group)
        targets = [str(value) for value in group["target_ids"]]
        paid_values = [data.target_amount(record_id) for record_id in targets]
        if any(value is None for value in paid_values):
            result.fail(f"{group['event_id']}: a target record is missing from the sources")
            continue
        paid = sum(value for value in paid_values if value is not None)

        if match_type == "partial":
            expected = sum(invoice.base_amount_minor for invoice in invoices)
            if paid != expected:
                result.fail(f"{group['event_id']}: partial instalments {paid} != {expected}")
            if len(targets) not in (2, 3, 4):
                result.fail(f"{group['event_id']}: partial has {len(targets)} instalments")
        elif match_type == "consolidated":
            expected = sum(invoice.base_amount_minor for invoice in invoices) - sum(
                note.base_amount_minor for note in notes
            )
            if paid != expected:
                result.fail(f"{group['event_id']}: consolidated {paid} != {expected}")
            if len(group["source_ids"]) > 4:
                result.fail(f"{group['event_id']}: consolidated source side exceeds 4 ids")
        elif subtype == "rounding":
            expected = sum(invoice.base_amount_minor for invoice in invoices)
            residual = paid - expected
            if not ROUNDING_RESIDUAL[0] <= abs(residual) <= ROUNDING_RESIDUAL[1]:
                result.fail(f"{group['event_id']}: rounding residual {residual} outside 1..5")
            if abs(residual) <= Thresholds.EXACT_SUM_TOL:
                result.fail(f"{group['event_id']}: rounding residual {residual} within tolerance")
        elif subtype == "short_pay":
            gross = sum(invoice.base_amount_minor for invoice in invoices)
            if gross <= 0 or paid <= 0:
                result.fail(f"{group['event_id']}: short_pay has a non-positive amount")
            elif low < ratio(paid, gross) < high:
                result.fail(
                    f"{group['event_id']}: short_pay net/gross {ratio(paid, gross)} "
                    f"is inside FEE_WINDOW"
                )
        elif match_type == "fx":
            _check_fx_group(data, group, invoices, result)
        elif match_type in {"clean", "name", "duplicate"} or subtype == "dba":
            expected = sum(invoice.base_amount_minor for invoice in invoices)
            if paid != expected:
                result.fail(f"{group['event_id']}: {match_type} paid {paid} != {expected}")


def _check_fx_group(
    data: Dataset, group: dict[str, Any], invoices: list[Any], result: Result
) -> None:
    """The payment must reproduce from the rate table on the date the record states (§4.7)."""
    if len(invoices) != 1:
        result.fail(f"{group['event_id']}: fx group holds {len(invoices)} invoices")
        return
    invoice = invoices[0]
    if invoice.currency == "GBP":
        result.fail(f"{group['event_id']}: fx invoice is in the base currency")
        return
    for record_id in (str(value) for value in group["target_ids"]):
        paid = data.target_amount(record_id)
        value_date = data.target_date(record_id)
        if paid is None or value_date is None:
            result.fail(f"{group['event_id']}: fx target {record_id} missing")
            continue
        rate = data.fx.get(value_date, {}).get(invoice.currency)
        if rate is None:
            result.fail(f"{group['event_id']}: no {invoice.currency} rate for {value_date}")
            continue
        if record_id.startswith("bank:"):
            # Converted to GBP on the value date.
            if paid != to_base_minor(invoice.amount_minor, rate):
                result.fail(
                    f"{group['event_id']}: bank {paid} != {invoice.amount_minor} "
                    f"{invoice.currency} at the {value_date} rate"
                )
        else:
            # Settled in the invoice currency, with that day's rate recorded on the line.
            row = data.settlement_by_id()[record_id]
            if paid != invoice.amount_minor:
                result.fail(f"{group['event_id']}: settlement {paid} != {invoice.amount_minor}")
            if str(row["currency"]) != invoice.currency:
                result.fail(f"{group['event_id']}: settlement in {row['currency']}")
            if str(row.get("fx_rate_date")) != value_date.isoformat():
                result.fail(f"{group['event_id']}: fx_rate_date is not the settlement date")


def check_arithmetic(data: Dataset) -> Result:
    result = Result("3. arithmetic")
    _check_settlement_arithmetic(data, result)
    _check_batches(data, result)
    _check_group_sums(data, result)
    result.note(f"{len(data.settlements)} settlements in {len(data.batches)} batches")
    foreign = sum(1 for batch in data.batches if str(batch["currency"]) != "GBP")
    result.note(f"{foreign} foreign-currency batches")
    return result


# ---------------------------------------------------------------------------- 4. dates


def check_dates(data: Dataset) -> Result:
    result = Result("4. dates")
    spanning = 0
    timing_total = 0
    for group in data.events():
        match_type = str(group["match_type"])
        subtype = subtype_of(group)
        invoices = data.invoices_of(group)
        if not invoices:
            continue
        dates = [data.target_date(str(value)) for value in group["target_ids"]]
        if any(value is None for value in dates):
            result.fail(f"{group['event_id']}: a target record has no date")
            continue
        value_dates = [value for value in dates if value is not None]
        earliest_due = min(invoice.due_date for invoice in invoices)

        if match_type == "timing":
            for value_date in value_dates:
                timing_total += 1
                lag = business_days_between(earliest_due, value_date)
                if not TIMING_LAG[0] <= lag <= TIMING_LAG[1]:
                    result.fail(f"{group['event_id']}: timing lag {lag}bd outside {TIMING_LAG}")
                if spans_non_business_day(earliest_due, value_date):
                    spanning += 1
        elif match_type == "partial":
            _check_partial_dates(group, value_dates, result)
        elif match_type in ON_TIME_TYPES or match_type == "consolidated" or subtype is not None:
            limit = add_business_days(earliest_due, 1)
            for value_date in value_dates:
                if value_date > limit:
                    result.fail(
                        f"{group['event_id']}: {_label(group)} paid {value_date} "
                        f"after due+1bd {limit}"
                    )

        # A payment cannot predate the invoice it settles.
        last_issue = max(invoice.issue_date for invoice in invoices)
        for value_date in value_dates:
            if value_date < last_issue:
                result.fail(
                    f"{group['event_id']}: paid {value_date} before invoice issue {last_issue}"
                )

    _check_credit_note_dates(data, result)

    if timing_total:
        share = spanning * 100 // timing_total
        result.note(f"timing spanning a non-business day: {spanning}/{timing_total} ({share}%)")
        if share < 50:
            result.fail(f"only {share}% of timing cases span a non-business day, need >=50%")
    return result


def _check_partial_dates(group: dict[str, Any], value_dates: list[date], result: Result) -> None:
    """Instalments 1-45 days apart (spec 01 §4.5)."""
    ordered = sorted(value_dates)
    for earlier, later in pairwise(ordered):
        gap = (later - earlier).days
        if not PARTIAL_GAP_DAYS[0] <= gap <= PARTIAL_GAP_DAYS[1]:
            result.fail(f"{group['event_id']}: instalments {gap} days apart, outside 1..45")


def _check_credit_note_dates(data: Dataset, result: Result) -> None:
    """A credit note is issued before the payment, and never in the year after the window.

    `reasoning(b)` cites a `CN-<next year>-*` ref precisely because it does not exist; a real
    one with that ref would make the class resolvable from the ERP alone (spec 01 §4.8).
    """
    paid_by_note: dict[str, date] = {}
    for group in data.events():
        for note in data.credit_notes_of(group):
            paid_by_note[note.ref] = date.fromisoformat(str(group["paid_on"]))
    years = {invoice.issue_date.year for invoice in data.erp.invoices}
    forbidden = max(years) + 1 if years else 0
    for note in data.erp.credit_notes:
        if note.issued_on.year == forbidden:
            result.fail(f"{note.ref}: a credit note dated {forbidden} must not exist")
        paid_on = paid_by_note.get(note.ref)
        if paid_on is not None and note.issued_on >= paid_on:
            result.fail(f"{note.ref}: issued {note.issued_on}, not before paid_on {paid_on}")


# ---------------------------------------------------------------------------- 5. names


def check_names(data: Dataset) -> Result:
    result = Result("5. names")
    counterparties = data.erp.counterparty_by_id()
    for group in data.events():
        match_type = str(group["match_type"])
        subtype = subtype_of(group)
        invoices = data.invoices_of(group)
        if not invoices:
            continue
        cp = counterparties[invoices[0].counterparty_id]
        names = (cp.legal_name, cp.trading_name)
        for record_id in (str(value) for value in group["target_ids"]):
            surface = data.payer_surface(record_id)
            if surface is None:
                continue
            score = surface_similarity(names, surface)

            if subtype == "dba":
                # Asserted *unsolvable* by fuzzy match, on both ERP names (§4.8a).
                if score >= Thresholds.NAME_SIM_MIN:
                    result.fail(
                        f"{record_id}: dba payer {surface!r} scores {score} against "
                        f"{cp.legal_name!r}, at or above NAME_SIM_MIN"
                    )
            elif match_type == "name":
                if score < Thresholds.NAME_SIM_MIN:
                    result.fail(
                        f"{record_id}: name variant {surface!r} scores {score} against "
                        f"{cp.legal_name!r}, below NAME_SIM_MIN"
                    )
            elif not normalises_to_any((cp.legal_name,), surface):
                result.fail(f"{record_id}: {surface!r} does not normalise to {cp.legal_name!r}")
    return result


# -------------------------------------------------------------------------- 6. leakage


def check_leakage(data: Dataset) -> Result:
    result = Result("6. leakage")
    for group in data.events():
        decoys = [str(value) for value in (group.get("decoys") or [])]
        if decoys and str(group["match_type"]) not in DECOY_TYPES:
            result.fail(f"{group['event_id']}: {group['match_type']} carries decoys {decoys}")
        for tag in decoys:
            if tag not in DECOY_TAGS:
                result.fail(f"{group['event_id']}: unknown decoy tag {tag!r}")
    tags = Counter(str(tag) for group in data.events() for tag in (group.get("decoys") or []))
    for tag, count in sorted(tags.items()):
        result.note(f"decoy/{tag}={count}")

    _check_pdf_rates(data, result)
    _check_template_spread(data, result)
    return result


def _check_pdf_rates(data: Dataset, result: Result) -> None:
    """§6.4: `has_pdf` must not be a classifier.

    Mandatory classes are at 100% by definition. Every other class that may carry an advice is
    compared against the rate they carry *collectively* -- see the note in
    `crossfoot.generate.remittance` on why that, and not the 40% global rate, is the reference.
    """
    with_pdf: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    for group in data.events():
        label = _label(group)
        totals[label] += 1
        if group.get("evidence_ids"):
            with_pdf[label] += 1

    for label, total in sorted(totals.items()):
        if not total:
            continue
        share = with_pdf[label] * 100 // total
        result.note(f"pdf/{label}={with_pdf[label]}/{total} ({share}%)")

    mandatory = [*MANDATORY_TYPES, *(f"reasoning/{name}" for name in MANDATORY_SUBTYPES)]
    for label in mandatory:
        if totals[label] and with_pdf[label] != totals[label]:
            result.fail(f"{label}: {with_pdf[label]}/{totals[label]} carry an advice, must be all")

    never = [*NEVER_TYPES]
    for label in never:
        if with_pdf[label]:
            result.fail(f"{label}: {with_pdf[label]} carry an advice, must be none")

    toppable = [*TOPPABLE_TYPES, *(f"reasoning/{name}" for name in TOPPABLE_SUBTYPES)]
    pooled_total = sum(totals[label] for label in toppable)
    pooled_with = sum(with_pdf[label] for label in toppable)
    if not pooled_total:
        return
    # Basis points throughout, so a percentage-point comparison never goes through a float.
    pooled_bp = pooled_with * 10_000 // pooled_total
    result.note(f"pooled non-mandatory pdf rate={_pct(pooled_bp)}")
    tolerance_bp = PDF_RATE_TOLERANCE_PP * 100
    for label in toppable:
        total = totals[label]
        if not total:
            continue
        if total < MIN_BUCKET_FOR_RATE_CHECK:
            # One event moves the rate by 100/total pp, so below this size the tolerance is
            # finer than the data's own resolution and the assertion could never pass. Both
            # real datasets clear it: the full set's smallest toppable bucket is fx at 300, and
            # a slice's is reasoning/dba at 36 (spec 01 §8.1). Small test worlds do not.
            result.note(f"pdf rate check skipped for {label}: only {total} events")
            continue
        share_bp = with_pdf[label] * 10_000 // total
        if abs(share_bp - pooled_bp) > tolerance_bp:
            result.fail(
                f"{label}: pdf rate {_pct(share_bp)} is more than {PDF_RATE_TOLERANCE_PP}pp "
                f"from the pooled {_pct(pooled_bp)}"
            )

    events = len(data.events())
    if events:
        global_bp = sum(with_pdf.values()) * 10_000 // events
        result.note(f"global pdf rate={_pct(global_bp)} (target ~{GLOBAL_TARGET_PCT}%)")


def _pct(basis_points: int) -> str:
    """`3372` -> `33.7%`."""
    whole, part = divmod(basis_points, 100)
    return f"{whole}.{part // 10}%"


def _check_template_spread(data: Dataset, result: Result) -> None:
    """Every layout must actually appear, held-out D included (spec 01 §6)."""
    templates = Counter(str(row["template"]) for row in data.remit)
    for name, count in sorted(templates.items()):
        result.note(f"template/{name}={count}")
    for name in ("A", "B", "C", HELD_OUT_TEMPLATE):
        if not templates.get(name):
            result.fail(f"template {name} appears on no remittance advice")


# ------------------------------------------------------------------ 7. money and dates


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

    for batch in data.batches:
        for key in ("total_net_minor", "payout_base_minor", "count"):
            if not isinstance(batch[key], int):
                result.fail(f"{batch['id']}: {key} is {type(batch[key]).__name__}, not int")
        for key in ("closed_on", "paid_out_on"):
            if not DATE_RE.match(str(batch[key])):
                result.fail(f"{batch['id']}: {key} {batch[key]} is not a calendar date")

    for row in data.remit:
        if not isinstance(row["total_minor"], int):
            result.fail(f"{row['id']}: total_minor is not an int")
        if not DATE_RE.match(str(row["paid_on"])):
            result.fail(f"{row['id']}: paid_on {row['paid_on']} is not a calendar date")
        lines = row.get("lines") or []
        if not lines:
            result.fail(f"{row['id']}: an advice with no line items")
        for line in lines:
            if not isinstance(line["amount_minor"], int):
                result.fail(f"{row['id']}: a line amount is not an int")
        total = sum(int(line["amount_minor"]) for line in lines)
        if total != int(row["total_minor"]):
            result.fail(f"{row['id']}: lines sum to {total}, total_minor says {row['total_minor']}")

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


def per_type_table(data: Dataset) -> list[str]:
    """One row per class: count, how many carry an advice, how many carry a decoy (§10)."""
    totals: Counter[str] = Counter()
    with_pdf: Counter[str] = Counter()
    with_decoy: Counter[str] = Counter()
    for group in data.events():
        label = _label(group)
        totals[label] += 1
        if group.get("evidence_ids"):
            with_pdf[label] += 1
        if group.get("decoys"):
            with_decoy[label] += 1

    extra: Counter[str] = Counter()
    for group in data.groups:
        if group.get("event_id"):
            continue
        kind = str(group.get("kind") or group["match_type"])
        extra[kind] += 1

    width = max((len(name) for name in [*totals, *extra]), default=4)
    rows = [f"{'type'.ljust(width)}  {'count':>6}  {'pdf':>6}  {'pdf%':>5}  {'decoy':>6}"]
    rows.append("-" * len(rows[0]))
    for label in sorted(totals):
        count = totals[label]
        share = with_pdf[label] * 100 // count if count else 0
        rows.append(
            f"{label.ljust(width)}  {count:>6}  {with_pdf[label]:>6}  "
            f"{share:>4}%  {with_decoy[label]:>6}"
        )
    rows.append("-" * len(rows[0]))
    rows.append(f"{'EVENTS'.ljust(width)}  {sum(totals.values()):>6}")
    for label in sorted(extra):
        rows.append(f"{('+ ' + label).ljust(width)}  {extra[label]:>6}")
    return rows


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    path = Path(args[0]) if args else Path("data/full")
    if not (path / "manifest.jsonl").exists():
        print(f"error: {path}/manifest.jsonl not found", file=sys.stderr)
        return 2

    data = load(path)
    header = data.header
    print(f"{path}  seed={header['seed']} kind={header['kind']} events={header['event_count']}")
    print(f"generator_sha={header['generator_sha']}\n")
    for row in per_type_table(data):
        print(f"  {row}")
    print()

    results = [check(data) for check in CHECKS]
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

    if PENDING:
        print("\npending:")
        for item in PENDING:
            print(f"    . {item}")

    passed = sum(1 for result in results if result.ok)
    print(f"\n{passed}/{len(results)} checks ok")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""UK current-account CSV export, one file per month (spec 01 §3.2).

The CSV carries no timestamp column, as a real export does not. The local-time sidecar
`posted_at.jsonl` exists so UTC normalisation is exercised at ingest, DST switch included.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from crossfoot.generate.emit.refs import description_for
from crossfoot.generate.records import Event, NoiseRecord, PaymentLeg
from crossfoot.generate.rng import substream
from crossfoot.money import format_minor

LONDON: Final = ZoneInfo("Europe/London")
OPENING_BALANCE_MINOR: Final = 1_250_000
HEADER: Final = "Transaction Date,Transaction Type,Description,Paid Out,Paid In,Balance"


@dataclass(slots=True)
class BankRow:
    leg: PaymentLeg
    description: str
    paid_in_minor: int = 0
    paid_out_minor: int = 0
    balance_minor: int = 0


@dataclass(slots=True)
class BankPlan:
    months: list[str] = field(default_factory=list)
    rows: dict[str, list[BankRow]] = field(default_factory=dict)

    def all_rows(self) -> list[BankRow]:
        return [row for month in self.months for row in self.rows[month]]


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _bucket(collected: dict[str, list[BankRow]], day: date, what: str) -> list[BankRow]:
    """The month file a row belongs in.

    The export is a fixed set of monthly files (spec 01 §3.2), so a row dated outside them is a
    bug in whatever chose the date -- `calendars.last_payable_day` is the ceiling. Say so,
    rather than raising a bare KeyError from a dict lookup.
    """
    key = _month_key(day)
    rows = collected.get(key)
    if rows is None:
        raise AssertionError(f"{what} dated {day} falls outside the bank export months")
    return rows


def month_range(start: date, end: date) -> list[str]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def plan(seed: int, events: list[Event], payouts: list[NoiseRecord], months: list[str]) -> BankPlan:
    """Assign bank line ids, row order and the running balance in place."""
    collected: dict[str, list[BankRow]] = {month: [] for month in months}

    for event in events:
        # Duplicates are real rows in the export; they just belong to their own group (§4.9).
        for leg in event.all_legs:
            if leg.kind != "bank":
                continue
            # An `unknown_receipt` orphan has no invoice at all, so there is no reference for
            # the payer to have quoted (spec 01 §4.10).
            invoice = event.invoice_for(leg)
            row = BankRow(
                leg=leg, description=description_for(leg, invoice.ref if invoice else None)
            )
            row.paid_in_minor = leg.amount_minor
            _bucket(collected, leg.value_date, f"{event.event_id} payment").append(row)

    for noise in payouts:
        row = BankRow(leg=noise.leg, description=description_for(noise.leg, None))
        if noise.leg.txn_type == "FPO":
            row.paid_out_minor = noise.leg.amount_minor
        else:
            row.paid_in_minor = noise.leg.amount_minor
        _bucket(collected, noise.leg.value_date, f"{noise.kind} line").append(row)

    balance = OPENING_BALANCE_MINOR
    ordered: dict[str, list[BankRow]] = {}
    for month in months:
        by_day: dict[date, list[BankRow]] = {}
        for row in collected[month]:
            by_day.setdefault(row.leg.value_date, []).append(row)
        rows: list[BankRow] = []
        for day in sorted(by_day):
            same_day = by_day[day]
            substream(seed, f"bank-order:{day.isoformat()}").shuffle(same_day)
            rows.extend(same_day)
        for counter, row in enumerate(rows, 1):
            row.leg.record_id = f"bank:{row.leg.value_date.isoformat()}:{counter:06d}"
            balance += row.paid_in_minor - row.paid_out_minor
            row.balance_minor = balance
        ordered[month] = rows
    return BankPlan(months=months, rows=ordered)


def write(out_dir: Path, bank_plan: BankPlan) -> None:
    out = out_dir / "bank"
    out.mkdir(parents=True, exist_ok=True)
    for month in bank_plan.months:
        lines = [HEADER]
        for row in bank_plan.rows[month]:
            lines.append(
                ",".join(
                    (
                        row.leg.value_date.strftime("%d/%m/%Y"),
                        row.leg.txn_type,
                        row.description,
                        format_minor(row.paid_out_minor) if row.paid_out_minor else "",
                        format_minor(row.paid_in_minor) if row.paid_in_minor else "",
                        format_minor(row.balance_minor),
                    )
                )
            )
        (out / f"{month}.csv").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")

    sidecar = []
    for row in bank_plan.all_rows():
        hour, minute = row.leg.posted_time
        posted = datetime(
            row.leg.value_date.year,
            row.leg.value_date.month,
            row.leg.value_date.day,
            hour,
            minute,
            tzinfo=LONDON,
        )
        sidecar.append(
            (
                row.leg.record_id,
                json.dumps(
                    {"id": row.leg.record_id, "posted_at": posted.isoformat()},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    body = "\n".join(text for _, text in sorted(sidecar))
    (out / "posted_at.jsonl").write_text(
        body + "\n" if body else "", encoding="utf-8", newline="\n"
    )

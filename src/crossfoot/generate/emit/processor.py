"""Processor settlements and batch payouts (spec 01 §3.3).

Settlement ids are content-free hex derived from (seed, event, leg), so a type's ids do not
move when another type is added. Batches are formed per settlement date and pay out into the
bank on the next business day as a single TFR line.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from crossfoot.generate.calendars import next_business_day
from crossfoot.generate.emit.refs import reference_fragment
from crossfoot.generate.records import Event, NoiseRecord, PaymentLeg
from crossfoot.generate.rng import digest_hex, substream
from crossfoot.generate.types.common import local_time

BATCH_MIN: Final = 48
BATCH_JITTER: Final = 4
PROCESSOR_DESCRIPTION: Final = "PAYCO SETTLE"


@dataclass(slots=True)
class Batch:
    id: str
    closed_on: str
    paid_out_on: str
    count: int
    total_net_minor: int
    line_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProcessorPlan:
    batches: list[Batch]
    payouts: list[NoiseRecord]


def _settlement_id(seed: int, event: Event, position: int) -> str:
    return f"proc:stl_{digest_hex(seed, f'stl:{event.event_id}:{position}', 12)}"


def plan(seed: int, events: list[Event]) -> ProcessorPlan:
    """Assign settlement ids and batches in place; return the batches and their payout lines."""
    legs: list[tuple[PaymentLeg, Event]] = []
    seen: set[str] = set()
    for event in events:
        for position, leg in enumerate(event.legs):
            if leg.kind != "settlement":
                continue
            leg.record_id = _settlement_id(seed, event, position)
            if leg.record_id in seen:
                raise AssertionError(f"settlement id collision: {leg.record_id}")
            seen.add(leg.record_id)
            legs.append((leg, event))

    by_date: dict[str, list[PaymentLeg]] = {}
    for leg, _ in legs:
        by_date.setdefault(leg.value_date.isoformat(), []).append(leg)

    batches: list[Batch] = []
    payouts: list[NoiseRecord] = []
    for day in sorted(by_date):
        rows = sorted(by_date[day], key=lambda leg: leg.record_id)
        rng = substream(seed, f"batch:{day}")
        size = BATCH_MIN + rng.randrange(BATCH_JITTER + 1)
        for chunk_index in range(0, len(rows), size):
            chunk = rows[chunk_index : chunk_index + size]
            batch_id = f"bat_{day}_{chunk_index // size + 1:02d}"
            paid_out_on = next_business_day(chunk[0].value_date)
            total_net = sum(leg.net_minor for leg in chunk)
            for leg in chunk:
                leg.batch_id = batch_id
            batches.append(
                Batch(
                    id=batch_id,
                    closed_on=day,
                    paid_out_on=paid_out_on.isoformat(),
                    count=len(chunk),
                    total_net_minor=total_net,
                    line_ids=[leg.record_id for leg in chunk],
                )
            )
            payouts.append(
                NoiseRecord(
                    kind="batch_payout",
                    leg=PaymentLeg(
                        kind="bank",
                        value_date=paid_out_on,
                        amount_minor=total_net,
                        currency="GBP",
                        payer_surface=f"{PROCESSOR_DESCRIPTION} {batch_id.upper()}",
                        txn_type="TFR",
                        posted_time=local_time(rng),
                    ),
                    notes=f"batch={batch_id}",
                )
            )
    return ProcessorPlan(batches=batches, payouts=payouts)


def write(out_dir: Path, events: list[Event], processor_plan: ProcessorPlan) -> None:
    out = out_dir / "processor"
    out.mkdir(parents=True, exist_ok=True)

    lines: list[tuple[str, str]] = []
    for event in events:
        for leg in event.legs:
            if leg.kind != "settlement":
                continue
            invoice = event.invoices[leg.invoice_index]
            payload = {
                "batch_id": leg.batch_id,
                "created_at": _iso_z(leg),
                "currency": leg.currency,
                "fee_minor": leg.fee_minor,
                "gross_minor": leg.amount_minor,
                "id": leg.record_id,
                "net_minor": leg.net_minor,
                "payer_name": leg.payer_surface,
                "payer_reference": reference_fragment(leg, invoice.ref),
                "product": leg.product,
                "settled_on": leg.value_date.isoformat(),
            }
            lines.append(
                (leg.record_id, json.dumps(payload, sort_keys=True, separators=(",", ":")))
            )
    body = "\n".join(text for _, text in sorted(lines))
    (out / "settlements.jsonl").write_text(
        body + "\n" if body else "", encoding="utf-8", newline="\n"
    )

    batch_lines = [
        json.dumps(
            {
                "closed_on": batch.closed_on,
                "count": batch.count,
                "id": batch.id,
                "paid_out_on": batch.paid_out_on,
                "total_net_minor": batch.total_net_minor,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for batch in sorted(processor_plan.batches, key=lambda item: item.id)
    ]
    text = "\n".join(batch_lines)
    (out / "batches.jsonl").write_text(text + "\n" if text else "", encoding="utf-8", newline="\n")


def _iso_z(leg: PaymentLeg) -> str:
    if leg.created_at is None:
        raise ValueError(f"settlement {leg.record_id} has no created_at")
    return leg.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")

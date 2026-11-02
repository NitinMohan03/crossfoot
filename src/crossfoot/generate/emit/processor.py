"""Processor settlements and batch payouts (spec 01 §3.3).

Settlement ids are content-free hex derived from (seed, event, leg), so a type's ids do not
move when another type is added. Batches are formed per **settlement date and currency** and pay
out into the bank on the next business day as a single TFR line -- see `Batch` for why the
currency is part of the key.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from crossfoot.generate.calendars import next_business_day
from crossfoot.generate.emit.refs import reference_fragment
from crossfoot.generate.fx import format_rate, to_base_minor
from crossfoot.generate.records import Event, NoiseRecord, PaymentLeg
from crossfoot.generate.rng import digest_hex, substream
from crossfoot.generate.types.common import local_time
from crossfoot.generate.world import World

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
    # Session 1.3: batches are keyed by (settled_on, currency). Spec 01 §3.3 pays a batch out
    # as one bank TFR line equal to `total_net_minor`, but §4.7 settles foreign events in
    # USD/EUR and the bank is a GBP account -- the two cannot both hold for a foreign batch. So
    # a batch records the currency it settled in, and `payout_base_minor` is what actually
    # lands in the bank: equal to `total_net_minor` for GBP, converted at the payout-date rate
    # otherwise. The validator checks both halves.
    currency: str = "GBP"
    payout_base_minor: int = 0
    line_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProcessorPlan:
    batches: list[Batch]
    payouts: list[NoiseRecord]


def _settlement_id(seed: int, event: Event, position: int) -> str:
    return f"proc:stl_{digest_hex(seed, f'stl:{event.event_id}:{position}', 12)}"


def plan(seed: int, events: list[Event], world: World) -> ProcessorPlan:
    """Assign settlement ids and batches in place; return the batches and their payout lines."""
    seen: set[str] = set()
    legs: list[PaymentLeg] = []
    for event in events:
        # `all_legs`, so a duplicated settlement line is a real line in the processor's export
        # and gets its own id, as a re-export would (§4.9).
        for position, leg in enumerate(event.all_legs):
            if leg.kind != "settlement":
                continue
            leg.record_id = _settlement_id(seed, event, position)
            if leg.record_id in seen:
                raise AssertionError(f"settlement id collision: {leg.record_id}")
            seen.add(leg.record_id)
            legs.append(leg)

    by_key: dict[tuple[str, str], list[PaymentLeg]] = {}
    for leg in legs:
        by_key.setdefault((leg.value_date.isoformat(), leg.currency), []).append(leg)

    batches: list[Batch] = []
    payouts: list[NoiseRecord] = []
    counters: dict[str, int] = {}
    for day, currency in sorted(by_key):
        rows = sorted(by_key[(day, currency)], key=lambda leg: leg.record_id)
        rng = substream(seed, f"batch:{day}:{currency}")
        size = BATCH_MIN + rng.randrange(BATCH_JITTER + 1)
        for chunk_index in range(0, len(rows), size):
            chunk = rows[chunk_index : chunk_index + size]
            # Numbered across the whole day, so a day's batch ids stay unique and contiguous
            # however many currencies settled in it.
            number = counters.get(day, 0) + 1
            counters[day] = number
            batch_id = f"bat_{day}_{number:02d}"
            paid_out_on = next_business_day(chunk[0].value_date)
            total_net = sum(leg.net_minor for leg in chunk)
            payout = (
                total_net
                if currency == "GBP"
                else to_base_minor(total_net, world.fx[paid_out_on][currency])
            )
            for leg in chunk:
                leg.batch_id = batch_id
            batches.append(
                Batch(
                    id=batch_id,
                    closed_on=day,
                    paid_out_on=paid_out_on.isoformat(),
                    count=len(chunk),
                    total_net_minor=total_net,
                    currency=currency,
                    payout_base_minor=payout,
                    line_ids=[leg.record_id for leg in chunk],
                )
            )
            payouts.append(
                NoiseRecord(
                    kind="batch_payout",
                    leg=PaymentLeg(
                        kind="bank",
                        value_date=paid_out_on,
                        amount_minor=payout,
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
        for leg in event.all_legs:
            if leg.kind != "settlement":
                continue
            invoice = event.invoice_for(leg)
            payload: dict[str, object] = {
                "batch_id": leg.batch_id,
                "created_at": _iso_z(leg),
                "currency": leg.currency,
                "fee_minor": leg.fee_minor,
                # The settlement-date rate for a foreign settlement (§4.7); null for GBP, so
                # the shape of a settlement line never varies.
                "fx_rate": None if leg.fx_rate_ppm is None else format_rate(leg.fx_rate_ppm),
                "fx_rate_date": None if leg.fx_rate_date is None else leg.fx_rate_date.isoformat(),
                "gross_minor": leg.amount_minor,
                "id": leg.record_id,
                "net_minor": leg.net_minor,
                "payer_name": leg.payer_surface,
                "payer_reference": reference_fragment(leg, invoice.ref) if invoice else None,
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
                "currency": batch.currency,
                "id": batch.id,
                "paid_out_on": batch.paid_out_on,
                "payout_base_minor": batch.payout_base_minor,
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

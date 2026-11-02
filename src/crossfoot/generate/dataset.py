"""End-to-end dataset build: world -> events -> refs -> ids -> files (spec 01 §11).

Pass order is not arbitrary. Invoice refs are globally sequential in issue-date order, so they
cannot be assigned until every event exists. Bank line ids carry the row number inside the day's
shuffle, so which copy of a duplicated payment is canonical is only knowable after the bank
emitter has run. And a remittance advice quotes invoice refs and advises on a payment, so it
comes last.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from crossfoot.generate import duplicates, manifest, remittance
from crossfoot.generate.calendars import HOLIDAY_FILE
from crossfoot.generate.decoys import DecoyPlan
from crossfoot.generate.emit import bank, erp, processor, remit
from crossfoot.generate.events import assign_refs, build_events
from crossfoot.generate.fx import render_fx_csv
from crossfoot.generate.noise import build_refunds
from crossfoot.generate.quotas import IMPLEMENTED
from crossfoot.generate.records import Event, NoiseRecord
from crossfoot.generate.world import World
from crossfoot.models.decision import MatchType

FULL_EVENTS = 10_000
SLICE_EVENTS = 1_200


@dataclass(slots=True)
class Dataset:
    world: World
    kind: str
    events: list[Event]
    type_counts: dict[MatchType, int]
    processor_plan: processor.ProcessorPlan
    bank_plan: bank.BankPlan
    decoy_plan: DecoyPlan
    refunds: list[NoiseRecord]

    @property
    def noise(self) -> list[NoiseRecord]:
        return self.processor_plan.payouts + self.refunds


def generate(
    seed: int,
    kind: str = "full",
    event_count: int | None = None,
    types: Sequence[MatchType] = IMPLEMENTED,
) -> Dataset:
    count = event_count or (FULL_EVENTS if kind == "full" else SLICE_EVENTS)
    world, events, type_counts, decoy_plan = build_events(seed, count, types, kind=kind)
    assign_refs(events)

    processor_plan = processor.plan(seed, events, world)
    refunds = build_refunds(seed, world, count)
    months = bank.month_range(world.invoice_start, world.payment_end)
    bank_plan = bank.plan(seed, events, processor_plan.payouts + refunds, months)

    # Needs the ids the two emitters just assigned (spec 01 §4.9).
    duplicates.resolve(events)

    remittance.assign(seed, events)
    remittance.build_all(seed, world, events, remittance.Corpus.load())

    return Dataset(
        world=world,
        kind=kind,
        events=events,
        type_counts=type_counts,
        processor_plan=processor_plan,
        bank_plan=bank_plan,
        decoy_plan=decoy_plan,
        refunds=refunds,
    )


def write(out_dir: Path, dataset: Dataset) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    erp.write(out_dir, dataset.world, dataset.events)
    processor.write(out_dir, dataset.events, dataset.processor_plan)
    bank.write(out_dir, dataset.bank_plan)
    remit.write(out_dir, dataset.events)

    (out_dir / "fx_rates.csv").write_text(
        render_fx_csv(dataset.world.fx), encoding="utf-8", newline="\n"
    )
    holidays = (files("crossfoot.generate.reference") / HOLIDAY_FILE).read_text(encoding="utf-8")
    (out_dir / HOLIDAY_FILE).write_text(holidays, encoding="utf-8", newline="\n")

    registry = [
        json.dumps(
            {"member_ids": list(group.member_ids), "parent_name": group.parent_name},
            sort_keys=True,
            separators=(",", ":"),
        )
        for group in sorted(dataset.world.parents, key=lambda item: item.parent_name)
    ]
    (out_dir / "parent_registry.jsonl").write_text(
        "\n".join(registry) + "\n" if registry else "", encoding="utf-8", newline="\n"
    )

    header = manifest.build_header(
        seed=dataset.world.seed,
        kind=dataset.kind,
        event_count=len(dataset.events),
        type_counts=dataset.type_counts,
        events=dataset.events,
        noise=dataset.noise,
    )
    manifest.write(out_dir, header, manifest.build_groups(dataset.events, dataset.noise))

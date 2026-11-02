"""End-to-end dataset build: world -> events -> refs -> ids -> files (spec 01 §11)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from crossfoot.generate import manifest
from crossfoot.generate.calendars import HOLIDAY_FILE
from crossfoot.generate.emit import bank, erp, processor
from crossfoot.generate.events import assign_refs, build_events
from crossfoot.generate.fx import render_fx_csv
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

    @property
    def noise(self) -> list[NoiseRecord]:
        return self.processor_plan.payouts


def generate(
    seed: int,
    kind: str = "full",
    event_count: int | None = None,
    types: Sequence[MatchType] = IMPLEMENTED,
) -> Dataset:
    count = event_count or (FULL_EVENTS if kind == "full" else SLICE_EVENTS)
    world, events, type_counts = build_events(seed, count, types)
    assign_refs(events)
    processor_plan = processor.plan(seed, events)
    months = bank.month_range(world.invoice_start, world.payment_end)
    bank_plan = bank.plan(seed, events, processor_plan.payouts, months)
    return Dataset(
        world=world,
        kind=kind,
        events=events,
        type_counts=type_counts,
        processor_plan=processor_plan,
        bank_plan=bank_plan,
    )


def write(out_dir: Path, dataset: Dataset) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    erp.write(out_dir, dataset.world, dataset.events)
    processor.write(out_dir, dataset.events, dataset.processor_plan)
    bank.write(out_dir, dataset.bank_plan)

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
        noise=dataset.noise,
    )
    manifest.write(out_dir, header, manifest.build_groups(dataset.events, dataset.noise))

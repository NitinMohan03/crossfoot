"""Quota allocation, per-event substreams, type dispatch, ref assignment (spec 01 §4, §9)."""

from collections.abc import Callable, Sequence
from random import Random

from crossfoot.generate.decoys import DecoyPlan, build_plan
from crossfoot.generate.quotas import IMPLEMENTED, allocate_for, reasoning_split, reasoning_subtype
from crossfoot.generate.records import Event
from crossfoot.generate.rng import substream
from crossfoot.generate.types import (
    clean,
    consolidated,
    duplicate,
    fee,
    fx,
    name,
    no_match,
    partial,
    reasoning,
    timing,
)
from crossfoot.generate.types.common import EventContext, build_pools
from crossfoot.generate.world import World, build_world
from crossfoot.models.decision import MatchType

Builder = Callable[[Random, EventContext], Event]

BUILDERS: dict[MatchType, Builder] = {
    "clean": clean.build,
    "fee": fee.build,
    "timing": timing.build,
    "name": name.build,
    "partial": partial.build,
    "consolidated": consolidated.build,
    "fx": fx.build,
    "reasoning": reasoning.build,
    "duplicate": duplicate.build,
    "no_match": no_match.build,
}


def build_events(
    seed: int,
    event_count: int,
    types: Sequence[MatchType] = IMPLEMENTED,
    world: World | None = None,
    kind: str = "full",
) -> tuple[World, list[Event], dict[MatchType, int], DecoyPlan]:
    """Allocate quotas, then build each event from its own substream."""
    missing = [name_ for name_ in types if name_ not in BUILDERS]
    if missing:
        raise NotImplementedError(f"no builder for {', '.join(missing)}")
    if world is None:
        world = build_world(seed, kind)
    pools = build_pools(world)
    counts = allocate_for(kind, event_count, types)
    plan = build_plan(seed, world, pools, counts, event_count)
    sub_split = reasoning_split(counts.get("reasoning", 0))

    slots: list[MatchType] = []
    for match_type in types:
        slots.extend([match_type] * counts[match_type])
    substream(seed, "master:slots").shuffle(slots)

    ordinals: dict[MatchType, int] = dict.fromkeys(types, 0)
    events: list[Event] = []
    for index, match_type in enumerate(slots):
        ordinal = ordinals[match_type]
        ordinals[match_type] = ordinal + 1

        subtype: str | None = None
        sub_ordinal = 0
        label = f"event:{match_type}:{ordinal}"
        if match_type == "reasoning":
            # Each sub-type gets its own substream block, so changing one sub-type's quota
            # cannot move another sub-type's records (spec 01 §9).
            subtype, sub_ordinal = reasoning_subtype(ordinal, sub_split)
            label = f"event:reasoning:{subtype}:{sub_ordinal}"

        ctx = EventContext(
            world=world,
            pools=pools,
            index=index,
            ordinal=ordinal,
            event_id=f"evt:{index + 1:06d}",
            decoy=plan.for_slot(match_type, ordinal),
            reasoning_subtype=subtype,
            sub_ordinal=sub_ordinal,
        )
        events.append(BUILDERS[match_type](substream(seed, label), ctx))
    return world, events, counts, plan


def assign_refs(events: Sequence[Event]) -> None:
    """Invoice and credit-note refs, sequential in issue-date order (spec 01 §2)."""
    invoices = [
        (invoice.issue_date, event.index, position, invoice)
        for event in events
        for position, invoice in enumerate(event.invoices)
    ]
    for number, (issue_date, _, _, invoice) in enumerate(
        sorted(invoices, key=lambda row: row[:3]), 1
    ):
        invoice.ref = f"INV-{issue_date.year}-{number:04d}"

    notes = [
        (note.issued_on, event.index, position, note)
        for event in events
        for position, note in enumerate(event.credit_notes)
    ]
    for number, (issued_on, _, _, note) in enumerate(sorted(notes, key=lambda row: row[:3]), 1):
        note.ref = f"CN-{issued_on.year}-{number:04d}"

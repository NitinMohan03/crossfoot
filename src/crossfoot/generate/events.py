"""Quota allocation, per-event substreams, type dispatch, ref assignment (spec 01 §4, §9)."""

from collections.abc import Callable, Sequence
from random import Random

from crossfoot.generate.quotas import IMPLEMENTED, allocate
from crossfoot.generate.records import Event
from crossfoot.generate.rng import substream
from crossfoot.generate.types import clean, fee, timing
from crossfoot.generate.types.common import EventContext, build_pools
from crossfoot.generate.world import World, build_world
from crossfoot.models.decision import MatchType

Builder = Callable[[Random, EventContext], Event]

BUILDERS: dict[MatchType, Builder] = {
    "clean": clean.build,
    "fee": fee.build,
    "timing": timing.build,
}


def build_events(
    seed: int,
    event_count: int,
    types: Sequence[MatchType] = IMPLEMENTED,
    world: World | None = None,
) -> tuple[World, list[Event], dict[MatchType, int]]:
    """Allocate quotas, then build each event from its own substream."""
    missing = [name for name in types if name not in BUILDERS]
    if missing:
        raise NotImplementedError(f"no builder for {', '.join(missing)} yet (Session 1.3)")
    if world is None:
        world = build_world(seed)
    pools = build_pools(world)
    counts = allocate(event_count, types)

    slots: list[MatchType] = []
    for name in types:
        slots.extend([name] * counts[name])
    substream(seed, "master:slots").shuffle(slots)

    ordinals: dict[MatchType, int] = dict.fromkeys(types, 0)
    events: list[Event] = []
    for index, match_type in enumerate(slots):
        ordinal = ordinals[match_type]
        ordinals[match_type] = ordinal + 1
        ctx = EventContext(
            world=world,
            pools=pools,
            index=index,
            ordinal=ordinal,
            event_id=f"evt:{index + 1:06d}",
        )
        events.append(BUILDERS[match_type](substream(seed, f"event:{match_type}:{ordinal}"), ctx))
    return world, events, counts


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

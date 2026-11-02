"""Exact type quotas (spec 01 §4).

Quotas are allocated, not sampled, so the validator can assert exact counts. Only the types a
session has implemented take part: Session 1.2 renormalises `clean`/`fee`/`timing` to 100%.
"""

from collections.abc import Sequence
from typing import Final

from crossfoot.models.decision import MatchType

NATURAL_RATES: Final[dict[MatchType, int]] = {
    "clean": 57,
    "fee": 10,
    "timing": 8,
    "name": 7,
    "partial": 5,
    "consolidated": 4,
    "fx": 3,
    "reasoning": 3,
    "duplicate": 2,
    "no_match": 1,
}

# Types with a builder today. Session 1.3 adds the rest.
IMPLEMENTED: Final[tuple[MatchType, ...]] = ("clean", "fee", "timing")

SLICE_FLOOR: Final = 100
SLICE_NATURAL_EVENTS: Final = 200


def allocate(event_count: int, types: Sequence[MatchType] = IMPLEMENTED) -> dict[MatchType, int]:
    """Split `event_count` over `types` in proportion to their natural rates.

    Largest-remainder rounding, so the counts sum exactly to `event_count` and the result is
    independent of dict iteration luck.
    """
    if event_count < 0:
        raise ValueError("event_count must not be negative")
    if not types:
        raise ValueError("no types to allocate over")
    total_rate = sum(NATURAL_RATES[name] for name in types)
    scaled = {name: event_count * NATURAL_RATES[name] for name in types}
    counts = {name: value // total_rate for name, value in scaled.items()}
    remainder = event_count - sum(counts.values())
    order = sorted(types, key=lambda name: (-(scaled[name] % total_rate), name))
    for name in order[:remainder]:
        counts[name] += 1
    return counts

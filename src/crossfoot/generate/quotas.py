"""Exact type quotas (spec 01 §4, §8.1).

Quotas are allocated, not sampled, so the validator can assert exact counts. Session 1.3
restored the natural rates: all ten types have a builder, so `IMPLEMENTED` is the whole table
and `allocate(10_000)` reproduces the plan's headline counts exactly.
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

IMPLEMENTED: Final[tuple[MatchType, ...]] = tuple(NATURAL_RATES)

SLICE_FLOOR: Final = 100
SLICE_NATURAL_EVENTS: Final = 200

# Order matters: `reasoning_split` gives the remainder to the earlier sub-types, which is how
# spec 01 §8.1 reads 106 reasoning events as 36/35/35.
REASONING_SUBTYPES: Final = ("dba", "short_pay", "rounding")


def _largest_remainder(
    event_count: int, weights: dict[str, int], order: Sequence[str]
) -> dict[str, int]:
    """Split `event_count` by integer `weights`, giving remainders out in `order`.

    Floor division plus largest-remainder top-up, so the parts sum exactly to `event_count` and
    the result never depends on dict iteration luck.
    """
    total_weight = sum(weights[name] for name in order)
    scaled = {name: event_count * weights[name] for name in order}
    counts = {name: scaled[name] // total_weight for name in order}
    remainder = event_count - sum(counts.values())
    position = {name: index for index, name in enumerate(order)}
    ranked = sorted(order, key=lambda name: (-(scaled[name] % total_weight), position[name]))
    for name in ranked[:remainder]:
        counts[name] += 1
    return counts


def allocate(event_count: int, types: Sequence[MatchType] = IMPLEMENTED) -> dict[MatchType, int]:
    """Split `event_count` over `types` in proportion to their natural rates."""
    if event_count < 0:
        raise ValueError("event_count must not be negative")
    if not types:
        raise ValueError("no types to allocate over")
    weights = {str(name): NATURAL_RATES[name] for name in types}
    counts = _largest_remainder(event_count, weights, [str(name) for name in types])
    return {name: counts[str(name)] for name in types}


def allocate_slice(
    event_count: int, types: Sequence[MatchType] = IMPLEMENTED
) -> dict[MatchType, int]:
    """Floor `SLICE_FLOOR` per type, then the rest at the natural rate (spec 01 §8.1).

    At 1,200 events over all ten types this is the spec's table exactly: clean 214, fee 120,
    timing 116, name 114, partial 110, consolidated 108, fx 106, reasoning 106, duplicate 104,
    no_match 102. Clean stays the plurality so slice-level aggregates resemble the full set.
    """
    floor_total = SLICE_FLOOR * len(types)
    if event_count < floor_total:
        raise ValueError(
            f"a slice of {event_count} cannot hold {SLICE_FLOOR} of each of {len(types)} types"
        )
    natural = allocate(event_count - floor_total, types)
    return {name: SLICE_FLOOR + natural[name] for name in types}


def allocate_for(
    kind: str, event_count: int, types: Sequence[MatchType] = IMPLEMENTED
) -> dict[MatchType, int]:
    """Quotas for a dataset `kind`: natural rates for `full`, floor-plus-natural for a slice."""
    if kind == "slice":
        return allocate_slice(event_count, types)
    return allocate(event_count, types)


def reasoning_split(event_count: int) -> dict[str, int]:
    """The three `reasoning` sub-types, equally weighted (spec 01 §4.8, §8.1).

    300 -> 100/100/100; 106 -> 36/35/35.
    """
    weights = dict.fromkeys(REASONING_SUBTYPES, 1)
    return _largest_remainder(event_count, weights, REASONING_SUBTYPES)


def reasoning_subtype(ordinal: int, split: dict[str, int]) -> tuple[str, int]:
    """Map a `reasoning` ordinal to `(subtype, ordinal within that subtype)`.

    Contiguous blocks in `REASONING_SUBTYPES` order, so a sub-type's substream labels -- and
    therefore its records -- do not move when another sub-type's quota changes.
    """
    cursor = ordinal
    for name in REASONING_SUBTYPES:
        size = split[name]
        if cursor < size:
            return name, cursor
        cursor -= size
    raise ValueError(f"reasoning ordinal {ordinal} exceeds the split {split}")

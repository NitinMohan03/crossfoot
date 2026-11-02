"""Quotas are allocated, not sampled, so the validator can assert exact counts (spec 01 §4)."""

import pytest

from crossfoot.generate.quotas import (
    IMPLEMENTED,
    NATURAL_RATES,
    REASONING_SUBTYPES,
    SLICE_FLOOR,
    allocate,
    allocate_for,
    allocate_slice,
    reasoning_split,
    reasoning_subtype,
)
from crossfoot.models.decision import MATCH_TYPES


def test_natural_rates_sum_to_one_hundred() -> None:
    assert sum(NATURAL_RATES.values()) == 100


def test_every_match_type_has_a_quota() -> None:
    assert set(IMPLEMENTED) == set(MATCH_TYPES)
    assert set(NATURAL_RATES) == set(MATCH_TYPES)


def test_full_set_reproduces_the_plan_counts() -> None:
    """The headline table in the plan and spec 01 §4, at 10,000 events."""
    assert allocate(10_000) == {
        "clean": 5_700,
        "fee": 1_000,
        "timing": 800,
        "name": 700,
        "partial": 500,
        "consolidated": 400,
        "fx": 300,
        "reasoning": 300,
        "duplicate": 200,
        "no_match": 100,
    }


def test_slice_reproduces_the_spec_table() -> None:
    """Floor 100 per type plus 200 at the natural rate (spec 01 §8.1)."""
    assert allocate_slice(1_200) == {
        "clean": 214,
        "fee": 120,
        "timing": 116,
        "name": 114,
        "partial": 110,
        "consolidated": 108,
        "fx": 106,
        "reasoning": 106,
        "duplicate": 104,
        "no_match": 102,
    }


def test_slice_keeps_clean_the_plurality() -> None:
    counts = allocate_slice(1_200)
    assert max(counts, key=lambda name: counts[name]) == "clean"
    assert min(counts.values()) >= SLICE_FLOOR


def test_allocate_for_dispatches_on_kind() -> None:
    assert allocate_for("full", 10_000) == allocate(10_000)
    assert allocate_for("slice", 1_200) == allocate_slice(1_200)


def test_allocation_is_exact_for_awkward_totals() -> None:
    for total in (1, 7, 250, 999, 1_200, 10_001):
        assert sum(allocate(total).values()) == total


def test_a_slice_below_the_floor_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot hold"):
        allocate_slice(999)


def test_reasoning_splits_into_three_sub_types() -> None:
    assert reasoning_split(300) == {"dba": 100, "short_pay": 100, "rounding": 100}
    # The spec's slice figure: 106 reasoning events as 36/35/35 (§8.1).
    assert reasoning_split(106) == {"dba": 36, "short_pay": 35, "rounding": 35}
    for total in (0, 1, 2, 7, 106, 300):
        assert sum(reasoning_split(total).values()) == total


def test_sub_type_blocks_are_contiguous_and_cover_the_quota() -> None:
    split = reasoning_split(106)
    seen: dict[str, list[int]] = {name: [] for name in REASONING_SUBTYPES}
    for ordinal in range(106):
        name, sub_ordinal = reasoning_subtype(ordinal, split)
        seen[name].append(sub_ordinal)
    for name, ordinals in seen.items():
        assert ordinals == list(range(split[name])), name


def test_an_ordinal_past_the_quota_is_refused() -> None:
    with pytest.raises(ValueError, match="exceeds the split"):
        reasoning_subtype(106, reasoning_split(106))

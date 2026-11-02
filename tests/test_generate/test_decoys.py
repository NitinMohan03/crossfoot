"""The §7 decoy planner: relations between events, so planned from the master stream."""

from crossfoot.generate.decoys import (
    AMOUNT_COLLISION_PAIRS,
    ELIGIBLE,
    NEAR_AMOUNT_PAIRS,
    RECURRING_COUNTERPARTIES,
    RECURRING_MONTHS,
    build_plan,
    scaled,
)
from crossfoot.generate.quotas import allocate
from crossfoot.generate.types.common import build_pools
from crossfoot.generate.world import build_world


def plan_for(seed: int, events: int):  # type: ignore[no-untyped-def]
    world = build_world(seed, "full")
    counts = allocate(events)
    return world, build_plan(seed, world, build_pools(world), counts, events)


def test_full_scale_hits_the_spec_targets() -> None:
    _, plan = plan_for(42, 10_000)
    counts = plan.counts()
    assert counts["amount_collision"] == AMOUNT_COLLISION_PAIRS * 2
    assert counts["near_amount"] == NEAR_AMOUNT_PAIRS * 2
    assert counts["recurring"] == RECURRING_COUNTERPARTIES * RECURRING_MONTHS


def test_decoys_land_only_on_eligible_types() -> None:
    _, plan = plan_for(42, 10_000)
    assert {match_type for match_type, _ in plan.specs} <= set(ELIGIBLE)


def test_one_spec_per_slot() -> None:
    _, plan = plan_for(42, 10_000)
    counts = plan.counts()
    assert len(plan.specs) == sum(counts.values())


def test_recurring_covers_twelve_distinct_months_per_counterparty() -> None:
    _, plan = plan_for(42, 10_000)
    months: dict[int, set[int]] = {}
    amounts: dict[int, set[int]] = {}
    for spec in plan.specs.values():
        if spec.tag != "recurring":
            continue
        months.setdefault(spec.counterparty_id, set()).add(spec.issue_date.month)
        amounts.setdefault(spec.counterparty_id, set()).add(spec.amount_minor)
    assert months, "no recurring decoys were planned"
    for counterparty_id, seen in months.items():
        assert seen == set(range(1, RECURRING_MONTHS + 1)), counterparty_id
        # "Only the date discriminates" (spec 01 §7).
        assert len(amounts[counterparty_id]) == 1, counterparty_id


def test_recurring_counterparties_can_be_invoiced_every_month() -> None:
    """A December invoice on 45- or 60-day terms would be paid outside the window."""
    world, plan = plan_for(42, 10_000)
    for spec in plan.specs.values():
        if spec.tag != "recurring":
            continue
        assert world.by_id(spec.counterparty_id).terms_days <= 30
        assert spec.issue_date.day <= 20


def test_amount_collisions_pair_different_counterparties_on_one_amount() -> None:
    _, plan = plan_for(42, 10_000)
    by_amount: dict[int, list[int]] = {}
    for spec in plan.specs.values():
        if spec.tag == "amount_collision":
            by_amount.setdefault(spec.amount_minor, []).append(spec.counterparty_id)
    assert by_amount
    for amount, members in by_amount.items():
        # Pairs are drawn independently, so an amount can recur; every pair is two distinct
        # counterparties, which is what makes the collision a hard negative.
        assert len(members) % 2 == 0, amount
        assert len(set(members)) > 1, amount


def test_near_amounts_pair_one_counterparty_within_two_percent() -> None:
    _, plan = plan_for(42, 10_000)
    pairs: dict[tuple[int, str], list[int]] = {}
    for spec in plan.specs.values():
        if spec.tag == "near_amount":
            key = (spec.counterparty_id, spec.issue_date.isoformat())
            pairs.setdefault(key, []).append(spec.amount_minor)
    assert pairs
    for (counterparty_id, _), amounts in pairs.items():
        if len(amounts) != 2:
            continue
        low, high = sorted(amounts)
        assert high - low <= high * 2 // 100 + 1, counterparty_id


def test_scaling_keeps_slice_proportions() -> None:
    assert scaled(AMOUNT_COLLISION_PAIRS, 10_000) == AMOUNT_COLLISION_PAIRS
    assert scaled(AMOUNT_COLLISION_PAIRS, 1_200) == 18
    assert scaled(RECURRING_COUNTERPARTIES, 1_200) == 2
    # Never zero, so even a small test world exercises the code path.
    assert scaled(RECURRING_COUNTERPARTIES, 250) == 1


def test_a_slice_world_can_still_be_planned() -> None:
    world = build_world(20240914, "slice")
    counts = allocate(1_200)
    plan = build_plan(20240914, world, build_pools(world), counts, 1_200)
    assert plan.counts()["recurring"] == 2 * RECURRING_MONTHS

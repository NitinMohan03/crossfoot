"""Hard negatives (spec 01 §7).

A decoy is a relation *between* events: two invoices with the same amount, two invoices 2%
apart, the same invoice every month. None of that can be drawn inside a single event's
substream, so it is planned here from one master substream and handed to the slots it
constrains. The builders then construct the event correctly from the start, which matters:
rewriting amounts or dates after the fact would invalidate the date and sum assertions each
builder makes about its own class.

Eligibility, per §7: decoys land on `clean`, `timing` and `name` only, so they never coincide
with an arithmetic class and muddy its boundary.

Session 1.3 narrows that further for the three *cross-event* decoys, which land on `clean` and
`name`: `timing` keeps `round_amount` only. `timing` searches over candidate issue dates to
satisfy its own ">=50% of cases span a non-business day" requirement (§4.3), and a forced issue
date from a decoy plan would fight that search. §7 says which types *may* carry a decoy, and
all three still do -- `round_amount` is drawn per-event in `make_invoice`.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random
from typing import Final

from crossfoot.generate.rng import substream
from crossfoot.generate.types.common import (
    DecoySpec,
    Pools,
    latest_issue_date,
    sample_amount_minor,
)
from crossfoot.generate.world import Counterparty, World
from crossfoot.models.decision import MatchType

# Targets at 10,000 events (spec 01 §7); slices carry the same proportions.
FULL_EVENT_BASIS: Final = 10_000
AMOUNT_COLLISION_PAIRS: Final = 150
NEAR_AMOUNT_PAIRS: Final = 150
RECURRING_COUNTERPARTIES: Final = 20
RECURRING_MONTHS: Final = 12

# Types a cross-event decoy may constrain, in a fixed order so the free-slot list is stable.
ELIGIBLE: Final[tuple[MatchType, ...]] = ("clean", "name")

# `near_amount` is "within 2% of each other": 10bp..200bp of the first amount.
NEAR_AMOUNT_BP: Final = (10, 200)
# `amount_collision` is "paid within +/-3 business days".
COLLISION_SPREAD_BD: Final = 3
# Business days back from the due date's last business day. Starting at 3 leaves room to come
# forward by up to `COLLISION_SPREAD_BD` without crossing the invoice date on 14-day terms.
BACK_BD_BASE: Final = 3

# A recurring counterparty needs all twelve invoices inside the payment window. Terms of 45 or
# 60 days push a December invoice past `payment_end`, so recurring draws from short terms only
# and issues on or before the 20th.
RECURRING_MAX_TERMS: Final = 30
RECURRING_MAX_DAY: Final = 20

Slot = tuple[MatchType, int]


@dataclass(slots=True)
class DecoyPlan:
    """Which event slots carry which decoy constraint."""

    specs: dict[Slot, DecoySpec] = field(default_factory=dict)

    def for_slot(self, match_type: MatchType, ordinal: int) -> DecoySpec | None:
        return self.specs.get((match_type, ordinal))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for spec in self.specs.values():
            out[spec.tag] = out.get(spec.tag, 0) + 1
        return out


def scaled(target: int, event_count: int) -> int:
    """A §7 target rescaled to this dataset's size, rounded down but never below one."""
    return max(1, target * event_count // FULL_EVENT_BASIS)


def _free_slots(rng: Random, counts: dict[MatchType, int]) -> list[Slot]:
    """Every eligible (type, ordinal) slot, shuffled, to be handed out in order."""
    slots: list[Slot] = [
        (match_type, ordinal)
        for match_type in ELIGIBLE
        for ordinal in range(counts.get(match_type, 0))
    ]
    rng.shuffle(slots)
    return slots


def _issue_window(world: World, cp: Counterparty) -> tuple[date, date]:
    return world.invoice_start, latest_issue_date(world, cp)


def _common_due_date(
    rng: Random, world: World, left: Counterparty, right: Counterparty
) -> date | None:
    """A due date both counterparties can reach from inside the invoice window.

    `due = issue + terms`, and the two counterparties have different terms, so a shared due date
    means different issue dates -- which is what makes the collision interesting: same amount,
    same payment week, nothing else in common.
    """
    lows: list[date] = []
    highs: list[date] = []
    for cp in (left, right):
        start, latest = _issue_window(world, cp)
        lows.append(start + timedelta(days=cp.terms_days))
        highs.append(latest + timedelta(days=cp.terms_days))
    low, high = max(lows), min(highs)
    if low > high:
        return None
    return low + timedelta(days=rng.randrange((high - low).days + 1))


def _plan_amount_collisions(
    rng: Random, world: World, pools: Pools, slots: list[Slot], pairs: int, plan: DecoyPlan
) -> None:
    """Two invoices, different counterparties, identical amount, paid within 3 business days."""
    members = pools.non_card.members
    if len(members) < 2:
        return
    for _ in range(pairs):
        if len(slots) < 2:
            return
        due: date | None = None
        left = right = members[0]
        for _attempt in range(32):
            left, right = rng.choice(members), rng.choice(members)
            if left.id == right.id:
                continue
            due = _common_due_date(rng, world, left, right)
            if due is not None:
                break
        if due is None:
            continue
        amount = sample_amount_minor(rng)
        back = BACK_BD_BASE + rng.randrange(COLLISION_SPREAD_BD + 1)
        apart = rng.randrange(COLLISION_SPREAD_BD + 1)
        for cp, offset in ((left, 0), (right, apart)):
            slot = slots.pop()
            plan.specs[slot] = DecoySpec(
                tag="amount_collision",
                counterparty_id=cp.id,
                issue_date=due - timedelta(days=cp.terms_days),
                amount_minor=amount,
                back_bd=back - offset,
            )


def _plan_near_amounts(
    rng: Random, world: World, pools: Pools, slots: list[Slot], pairs: int, plan: DecoyPlan
) -> None:
    """Same counterparty, two invoices within 2% of each other, paid a day apart."""
    members = pools.non_card.members
    if not members:
        return
    for _ in range(pairs):
        if len(slots) < 2:
            return
        cp = rng.choice(members)
        start, latest = _issue_window(world, cp)
        if latest < start:
            continue
        issue = start + timedelta(days=rng.randrange((latest - start).days + 1))
        first = sample_amount_minor(rng)
        delta = max(1, first * rng.randint(*NEAR_AMOUNT_BP) // 10_000)
        second = first + delta if rng.randrange(2) else max(1, first - delta)
        back = BACK_BD_BASE + rng.randrange(COLLISION_SPREAD_BD + 1)
        for amount, offset in ((first, 0), (second, 1)):
            slot = slots.pop()
            plan.specs[slot] = DecoySpec(
                tag="near_amount",
                counterparty_id=cp.id,
                issue_date=issue,
                amount_minor=amount,
                back_bd=back - offset,
            )


def _plan_recurring(
    rng: Random, world: World, pools: Pools, slots: list[Slot], wanted: int, plan: DecoyPlan
) -> None:
    """A fixed monthly invoice for all twelve months: only the date discriminates."""
    members = [cp for cp in pools.non_card.members if cp.terms_days <= RECURRING_MAX_TERMS]
    if not members:
        return
    # Without replacement: a counterparty drawn twice would carry two different monthly
    # amounts, and then the amount discriminates as well as the date, which is the opposite of
    # what this decoy is for (spec 01 §7).
    chosen = sorted(members, key=lambda cp: cp.id)
    rng.shuffle(chosen)
    year = world.invoice_start.year
    for cp in chosen[:wanted]:
        if len(slots) < RECURRING_MONTHS:
            return
        day = rng.randint(1, RECURRING_MAX_DAY)
        amount = sample_amount_minor(rng)
        for month in range(1, RECURRING_MONTHS + 1):
            slot = slots.pop()
            plan.specs[slot] = DecoySpec(
                tag="recurring",
                counterparty_id=cp.id,
                issue_date=date(year, month, day),
                amount_minor=amount,
            )


def build_plan(
    seed: int, world: World, pools: Pools, counts: dict[MatchType, int], event_count: int
) -> DecoyPlan:
    """Assign the §7 cross-event decoys to event slots, scaled to this dataset's size."""
    rng = substream(seed, "master:decoys")
    plan = DecoyPlan()
    slots = _free_slots(rng, counts)
    _plan_amount_collisions(
        rng, world, pools, slots, scaled(AMOUNT_COLLISION_PAIRS, event_count), plan
    )
    _plan_near_amounts(rng, world, pools, slots, scaled(NEAR_AMOUNT_PAIRS, event_count), plan)
    _plan_recurring(rng, world, pools, slots, scaled(RECURRING_COUNTERPARTIES, event_count), plan)
    return plan

"""`partial` -- 5% of events. One invoice settled in instalments (spec 01 §4.5).

N in {2,3,4} payment records summing **exactly** to `base_amount_minor`, spread 1-45 days apart
and allowed to cross a month end -- which is the point: the bank CSVs are per month, so a
matcher that reconciles one file at a time cannot close these.

The gaps are drawn *before* the invoice, so its issue date can reserve exactly the room the
whole schedule needs. Choosing the invoice first and clamping the instalments to the end of the
payment window is what the first cut did, and it put two instalments on the same day; snapping a
45-day gap forward onto a business day overshot the 45-day bound the same way. Both show up in
`validate` check 4, so both are constructed out rather than asserted away.

A consequence worth naming: a counterparty on 60-day terms that draws three long gaps can only
be invoiced in the first half of the year, so `partial` issue dates skew earlier than other
classes'. The spec constrains the gaps, not the issue-date distribution.

Always carries a remittance PDF listing the instalments (§4.5, §6.4).
"""

from datetime import timedelta
from random import Random
from typing import Final

from crossfoot.generate.calendars import business_day_on_or_after, last_payable_day
from crossfoot.generate.records import Event, PaymentLeg
from crossfoot.generate.types.common import (
    EventContext,
    make_invoice,
    on_time_paid_on,
    payment_leg,
    split_exact,
)

PARTS: Final = (2, 3, 4)
# The spec's bound on the gap between instalments, in calendar days.
GAP_BOUNDS: Final = (1, 45)
# Gaps are drawn against a lower cap: landing on a weekend or a Christmas holiday run snaps the
# date forward, and it is the *resulting* gap that has to stay inside GAP_BOUNDS.
GAP_DRAW_MAX: Final = 40
SNAP_SLACK_DAYS: Final = 5
# An instalment of at least GBP 1, so a 4-way split of a small invoice is still plausible.
INSTALMENT_FLOOR_MINOR: Final = 100


def build(rng: Random, ctx: EventContext) -> Event:
    cp = ctx.pools.non_card.pick(rng)
    parts = rng.choice(PARTS)
    gaps = [rng.randint(GAP_BOUNDS[0], GAP_DRAW_MAX) for _ in range(parts - 1)]
    reserve = sum(gaps) + SNAP_SLACK_DAYS * len(gaps)

    invoice, _ = make_invoice(rng, ctx.world, cp, extra_days=reserve)
    floor = min(INSTALMENT_FLOOR_MINOR, invoice.base_amount_minor // parts)
    amounts = split_exact(rng, invoice.base_amount_minor, parts, max(1, floor))

    legs: list[PaymentLeg] = []
    day = on_time_paid_on(rng, invoice)
    for position, amount in enumerate(amounts):
        if position:
            previous = day
            day = business_day_on_or_after(day + timedelta(days=gaps[position - 1]))
            gap = (day - previous).days
            if not GAP_BOUNDS[0] <= gap <= GAP_BOUNDS[1]:
                raise AssertionError(f"partial instalments {gap} days apart, outside {GAP_BOUNDS}")
        legs.append(payment_leg(rng, cp, invoice, value_date=day, amount_minor=amount))

    paid = sum(leg.amount_minor for leg in legs)
    if paid != invoice.base_amount_minor:
        raise AssertionError(f"partial instalments sum to {paid}, not {invoice.base_amount_minor}")
    ceiling = last_payable_day(ctx.world.payment_end)
    if legs[-1].value_date > ceiling:
        raise AssertionError(f"partial instalment {legs[-1].value_date} past {ceiling}")

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="partial",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=legs[-1].value_date,
        invoices=[invoice],
        legs=legs,
        has_pdf=True,
        notes=f"instalments={parts}",
    )

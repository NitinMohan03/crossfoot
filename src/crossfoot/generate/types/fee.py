"""`fee` -- 10% of events. Card-processor fee deducted before payout (spec 01 §4.2).

The bank never sees the gross: it sees one batch payout. Only the processor record closes the
gap, which is the point of the class.
"""

from random import Random

from crossfoot.generate.calendars import add_business_days
from crossfoot.generate.records import Event
from crossfoot.generate.types.common import (
    EventContext,
    make_invoice,
    on_time_paid_on,
    settlement_leg,
    value_date_for,
)


def build(rng: Random, ctx: EventContext) -> Event:
    cp = ctx.pools.card.pick(rng)
    invoice, decoys = make_invoice(rng, ctx.world, cp)
    paid_on = on_time_paid_on(rng, invoice)
    settled_on = value_date_for(rng, paid_on)

    leg = settlement_leg(
        rng, cp, invoice, settled_on=settled_on, gross_minor=invoice.base_amount_minor
    )
    if leg.fee_minor <= 0:
        raise AssertionError("fee event has no fee")

    limit = add_business_days(invoice.due_date, 1)
    if settled_on > limit:
        raise AssertionError(f"fee settlement {settled_on} later than due+1bd {limit}")

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="fee",
        counterparty_id=cp.id,
        payment_method="processor",
        paid_on=paid_on,
        invoices=[invoice],
        legs=[leg],
        decoys=decoys,
    )

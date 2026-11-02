"""`clean` -- 57% of events. Nothing wrong with them (spec 01 §4.1)."""

from random import Random

from crossfoot.generate.calendars import add_business_days
from crossfoot.generate.records import Event
from crossfoot.generate.types.common import (
    EventContext,
    bank_leg,
    make_invoice,
    on_time_paid_on,
    settlement_leg,
    value_date_for,
)


def build(rng: Random, ctx: EventContext) -> Event:
    cp = ctx.pools.non_card.pick(rng)
    invoice, decoys = make_invoice(rng, ctx.world, cp, allow_round_decoy=True)
    paid_on = on_time_paid_on(rng, invoice)
    value_date = value_date_for(rng, paid_on)

    if cp.payment_method == "direct":
        leg = bank_leg(rng, cp, value_date=value_date, amount_minor=invoice.base_amount_minor)
    else:
        leg = settlement_leg(
            rng, cp, invoice, settled_on=value_date, gross_minor=invoice.base_amount_minor
        )

    limit = add_business_days(invoice.due_date, 1)
    if value_date > limit:
        raise AssertionError(f"clean payment {value_date} later than due+1bd {limit}")

    return Event(
        index=ctx.index,
        event_id=ctx.event_id,
        match_type="clean",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=[invoice],
        legs=[leg],
        decoys=decoys,
    )

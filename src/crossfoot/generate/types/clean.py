"""`clean` -- 57% of events. Nothing wrong with them (spec 01 §4.1)."""

from random import Random

from crossfoot.generate.records import Event
from crossfoot.generate.types.common import (
    EventContext,
    assert_on_time,
    make_invoice,
    on_time_paid_on,
    payment_leg,
    value_date_for,
)


def build(rng: Random, ctx: EventContext) -> Event:
    decoy = ctx.decoy
    if decoy is None:
        cp = ctx.pools.non_card.pick(rng)
        invoice, decoys = make_invoice(rng, ctx.world, cp, allow_round_decoy=True)
    else:
        # A decoy pins the counterparty, the issue date and the amount, because the property
        # being constructed is a relation to *another* event (spec 01 §7).
        cp = ctx.world.by_id(decoy.counterparty_id)
        invoice, decoys = make_invoice(
            rng,
            ctx.world,
            cp,
            issue_date=decoy.issue_date,
            amount_minor=decoy.amount_minor,
        )
        decoys = [decoy.tag]

    paid_on = on_time_paid_on(rng, invoice, None if decoy is None else decoy.back_bd)
    value_date = value_date_for(rng, paid_on)

    leg = payment_leg(
        rng,
        cp,
        invoice,
        value_date=value_date,
        amount_minor=invoice.base_amount_minor,
    )
    assert_on_time(invoice, value_date, "clean")

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="clean",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=[invoice],
        legs=[leg],
        decoys=decoys,
    )

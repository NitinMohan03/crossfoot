"""`name` -- 7% of events. The payer name has drifted; amount and dates are clean (spec 01 §4.4).

The drift has to clear two bars at once, and the interesting one is the lower bar: a case that
`names.trivially_normalises` already resolves is a `clean` case wearing a different hat, and
would make this class free. So a bare legal-suffix swap is never used on its own --
`names.normalise` strips every legal suffix, so `LTD` -> `PLC` is a trivial difference.

Both bars are asserted against the string the *matcher* will read: for a bank line that is the
32-character CSV description, not the surface the builder constructed.
"""

from random import Random

from crossfoot.generate.records import Event
from crossfoot.generate.types.common import (
    EventContext,
    assert_drifted,
    assert_on_time,
    drifted_surface_name,
    make_invoice,
    on_time_paid_on,
    payment_leg,
    surface_as_written,
    value_date_for,
)


def build(rng: Random, ctx: EventContext) -> Event:
    decoy = ctx.decoy
    if decoy is None:
        cp = ctx.pools.non_card.pick(rng)
        invoice, decoys = make_invoice(rng, ctx.world, cp, allow_round_decoy=True)
    else:
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
    surface, style = drifted_surface_name(rng, cp, ctx.ordinal)

    leg = payment_leg(
        rng,
        cp,
        invoice,
        value_date=value_date,
        amount_minor=invoice.base_amount_minor,
        payer_surface=surface,
        preserve_name=True,
    )
    assert_on_time(invoice, value_date, "name")
    assert_drifted(cp, surface, surface_as_written(leg, invoice))

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="name",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=[invoice],
        legs=[leg],
        decoys=decoys,
        notes=f"drift={style}",
    )

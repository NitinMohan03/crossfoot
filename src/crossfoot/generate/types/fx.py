"""`fx` -- 3% of events. Three dates, three rates, one invoice (spec 01 §4.7).

The ERP books the foreign amount plus a GBP base at the **invoice-date** rate. Then:

  * a `direct` counterparty's bank credit arrives in GBP at the **value-date** rate, so it does
    not equal `base_amount_minor` -- the residual is whatever the walk did in between; or
  * a processor counterparty settles in the foreign currency and the **settlement-date** rate is
    recorded on the line.

The FX table ships with the dataset, so this class is rules-solvable: the residual is bounded by
a <=0.5%/business-day walk over a handful of days. A foreign currency implies `fx`, which is why
the pool is "foreign and card-free" -- a card product would already mean `fee` (§4.2).
"""

from random import Random

from crossfoot.generate.fx import to_base_minor
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
    cp = ctx.pools.foreign.pick(rng)
    invoice, _ = make_invoice(rng, ctx.world, cp)
    paid_on = on_time_paid_on(rng, invoice)
    value_date = value_date_for(rng, paid_on)
    rate_ppm = ctx.world.fx[value_date][cp.currency]

    if cp.payment_method == "direct":
        # The bank is a GBP account: the payer's bank converts on the value date.
        amount = to_base_minor(invoice.amount_minor, rate_ppm)
        leg = payment_leg(rng, cp, invoice, value_date=value_date, amount_minor=amount)
    else:
        # The processor settles in the invoice currency and records the rate it used.
        leg = payment_leg(
            rng,
            cp,
            invoice,
            value_date=value_date,
            amount_minor=invoice.amount_minor,
            fx_rate_ppm=rate_ppm,
        )

    if invoice.currency == "GBP":
        raise AssertionError("fx event drew a base-currency counterparty")
    if invoice.fx_rate_ppm is None or invoice.fx_rate_date != invoice.issue_date:
        raise AssertionError("fx invoice must carry the invoice-date rate")
    assert_on_time(invoice, value_date, "fx")

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="fx",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=[invoice],
        legs=[leg],
        notes=f"ccy={cp.currency} value_date_rate={rate_ppm}",
    )

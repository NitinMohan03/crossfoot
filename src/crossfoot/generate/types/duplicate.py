"""`duplicate` -- 2% of events. The payment record arrives twice (spec 01 §4.9).

What is duplicated is the **payment record**, not the invoice, so the class is independent of
payment method: a bank line for a `direct` counterparty, a settlement line for a processor one.
The copy is perturbed the way a re-export would be -- new record id, same amount and value
date, a different `posted_at`/`created_at`, and a reference style that re-truncates the
description differently.

Which copy is canonical cannot be decided here. Bank line ids carry the row number inside the
day's deterministic shuffle, so "the earlier id" is only known once `emit/bank.py` has assigned
them: `crossfoot.generate.duplicates.resolve` does that after the fact and may swap the two.
"""

from random import Random

from crossfoot.generate.records import Event, PaymentLeg
from crossfoot.generate.types.common import (
    EventContext,
    assert_on_time,
    local_time,
    make_invoice,
    on_time_paid_on,
    payment_leg,
    reference_choice,
    utc_moment,
    value_date_for,
)


def _copy_of(rng: Random, leg: PaymentLeg) -> PaymentLeg:
    """A re-export of `leg`: same money, same date, everything incidental redrawn."""
    style, po_number = reference_choice(rng, 50)
    created_at = (
        utc_moment(rng, leg.created_at.date(), 7, 22) if leg.created_at is not None else None
    )
    return PaymentLeg(
        kind=leg.kind,
        value_date=leg.value_date,
        amount_minor=leg.amount_minor,
        currency=leg.currency,
        payer_surface=leg.payer_surface,
        fee_minor=leg.fee_minor,
        net_minor=leg.net_minor,
        reference_style=style,
        po_number=po_number,
        txn_type=leg.txn_type,
        created_at=created_at,
        product=leg.product,
        posted_time=local_time(rng),
        invoice_index=leg.invoice_index,
        fx_rate_ppm=leg.fx_rate_ppm,
        fx_rate_date=leg.fx_rate_date,
        preserve_name=leg.preserve_name,
    )


def build(rng: Random, ctx: EventContext) -> Event:
    cp = ctx.pools.non_card.pick(rng)
    invoice, _ = make_invoice(rng, ctx.world, cp)
    paid_on = on_time_paid_on(rng, invoice)
    value_date = value_date_for(rng, paid_on)

    leg = payment_leg(
        rng,
        cp,
        invoice,
        value_date=value_date,
        amount_minor=invoice.base_amount_minor,
    )
    copy = _copy_of(rng, leg)
    assert_on_time(invoice, value_date, "duplicate")

    if copy.amount_minor != leg.amount_minor or copy.value_date != leg.value_date:
        raise AssertionError("a duplicate must carry the same amount and value date")

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="duplicate",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=[invoice],
        legs=[leg],
        duplicate_legs=[copy],
    )

"""`no_match` -- 1% of events, the orphans (spec 01 §4.10).

Two kinds, 50/50, and they are the only *events* that count toward the 1%:

  * `open_invoice`    -- an ERP invoice nobody paid: one source id, no targets.
  * `unknown_receipt` -- a bank credit with no invoice, no processor line and no PDF: no source
                         ids, one target.

The other two `no_match` kinds -- `batch_payout` and `refund` -- are noise records outside the
event quota, emitted by `emit/processor.py` and `emit/bank.py`. They are labelled all the same,
so the confusion matrix has a real no-match row and correct non-matches score (§5.1).
"""

from random import Random
from typing import Final

from crossfoot.generate.records import Event
from crossfoot.generate.types.common import (
    EventContext,
    bank_leg,
    make_invoice,
    on_time_paid_on,
    value_date_for,
)

KINDS: Final = ("open_invoice", "unknown_receipt")


def build(rng: Random, ctx: EventContext) -> Event:
    # Alternating rather than sampled, so the 50/50 split is exact at any quota.
    kind = KINDS[ctx.ordinal % len(KINDS)]
    cp = ctx.pools.non_card.pick(rng)

    if kind == "open_invoice":
        invoice, _ = make_invoice(rng, ctx.world, cp)
        return Event(
            index=ctx.index,
            ordinal=ctx.ordinal,
            event_id=ctx.event_id,
            match_type="no_match",
            counterparty_id=cp.id,
            payment_method=cp.payment_method,
            paid_on=invoice.due_date,
            invoices=[invoice],
            legs=[],
            no_match_kind=kind,
            notes="invoice never paid",
        )

    # A receipt with nothing behind it. An invoice is drafted only to place the credit somewhere
    # plausible in the calendar; it is never emitted, so the bank line stands alone.
    draft, _ = make_invoice(rng, ctx.world, cp)
    value_date = value_date_for(rng, on_time_paid_on(rng, draft))
    leg = bank_leg(
        rng,
        cp,
        value_date=value_date,
        amount_minor=draft.base_amount_minor,
        reference_present_pct=0,
    )
    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="no_match",
        counterparty_id=cp.id,
        payment_method="direct",
        paid_on=value_date,
        invoices=[],
        legs=[leg],
        no_match_kind=kind,
        notes="bank credit with no invoice",
    )

"""`consolidated` -- 4% of events. One payment, several invoices (spec 01 §4.6).

`payment = sum(invoices) - credit_note`, exactly. The credit-note variant fires 40% of the
time; the note is issued before `paid_on` and clamped to the invoice window's last day, because
`reasoning(b)` cites a `CN-2025-*` ref specifically to be unresolvable and one must never exist
(§4.8).

Session 1.3 resolution of §4.6's two bounds: the **source side never exceeds 4 ids**. So N is
2-4 invoices on its own, and 2-3 when a credit note joins them.
"""

from datetime import timedelta
from random import Random
from typing import Final

from crossfoot.generate.records import Event
from crossfoot.generate.types.common import (
    EventContext,
    assert_on_time,
    group_paid_on,
    make_credit_note,
    make_invoice_group,
    payment_leg,
    value_date_for,
)

GROUP_CAP: Final = 4
CREDIT_NOTE_PCT: Final = 40
# The note offsets 2-20% of the invoice total, so the payment stays comfortably positive.
CREDIT_NOTE_BP: Final = (200, 2_000)


def build(rng: Random, ctx: EventContext) -> Event:
    cp = ctx.pools.non_card.pick(rng)
    with_note = rng.randrange(100) < CREDIT_NOTE_PCT
    # One slot of the cap goes to the credit note when there is one.
    parts = rng.randint(2, GROUP_CAP - (1 if with_note else 0))

    invoices = make_invoice_group(rng, ctx.world, cp, parts)
    total = sum(invoice.base_amount_minor for invoice in invoices)
    paid_on = group_paid_on(rng, invoices)
    value_date = value_date_for(rng, paid_on)

    notes = []
    expected = total
    if with_note:
        offset = max(1, total * rng.randint(*CREDIT_NOTE_BP) // 10_000)
        # Strictly before `paid_on`, and never before the last invoice it offsets exists.
        # `group_paid_on` guarantees paid_on > last issue date, so this window is non-empty.
        last_issue = max(invoice.issue_date for invoice in invoices)
        span = (paid_on - last_issue).days
        note = make_credit_note(
            rng,
            ctx.world,
            cp,
            issued_on=paid_on - timedelta(days=1 + rng.randrange(max(1, span))),
            base_amount_minor=offset,
        )
        notes.append(note)
        expected = total - offset

    leg = payment_leg(
        rng,
        cp,
        invoices[0],
        value_date=value_date,
        amount_minor=expected,
    )

    if leg.amount_minor != expected:
        raise AssertionError(f"consolidated payment {leg.amount_minor} != expected {expected}")
    if len(invoices) + len(notes) > GROUP_CAP:
        raise AssertionError(f"consolidated source side holds {len(invoices) + len(notes)} ids")
    for note in notes:
        if note.issued_on > paid_on:
            raise AssertionError(f"credit note {note.issued_on} issued after paid_on {paid_on}")
    # On time against the earliest due date, so the whole group is inside clean's window.
    assert_on_time(min(invoices, key=lambda item: item.due_date), value_date, "consolidated")

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="consolidated",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=invoices,
        credit_notes=notes,
        legs=[leg],
        has_pdf=True,
        notes=f"invoices={parts} credit_note={'yes' if with_note else 'no'}",
    )

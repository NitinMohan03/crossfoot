"""`timing` -- 8% of events. Settlement lag, 2-5 business days past the due date (spec 01 §4.3).

Sharp against `clean`, which never exceeds due+1bd. At least half the cases span a weekend or
bank holiday, so calendar-day arithmetic gets the lag wrong.
"""

from datetime import date, timedelta
from random import Random

from crossfoot.generate.calendars import (
    add_business_days,
    business_days_between,
    spans_non_business_day,
)
from crossfoot.generate.records import Event
from crossfoot.generate.types.common import (
    EventContext,
    bank_leg,
    latest_issue_date,
    make_invoice,
    settlement_leg,
)
from crossfoot.generate.world import Counterparty, World

LAG_MIN = 2
LAG_MAX = 5
ATTEMPTS = 48


def _plan_dates(
    rng: Random, world: World, cp: Counterparty, base_issue: date, want_span: bool
) -> tuple[date, int]:
    """Pick (issue_date, lag) so that spanning a non-business day matches `want_span`.

    Half the cases must span one (§4.3), so the caller alternates `want_span` by ordinal
    rather than leaving the ratio to chance.
    """
    offset = rng.randrange(LAG_MAX - LAG_MIN + 1)
    latest = latest_issue_date(world, cp)
    for attempt in range(ATTEMPTS):
        lag = LAG_MIN + (attempt + offset) % (LAG_MAX - LAG_MIN + 1)
        shift = attempt // (LAG_MAX - LAG_MIN + 1)
        for sign in (1, -1):
            issue = base_issue + timedelta(days=shift * sign)
            if not world.invoice_start <= issue <= latest:
                continue
            due = issue + timedelta(days=cp.terms_days)
            value_date = add_business_days(due, lag)
            if spans_non_business_day(due, value_date) == want_span:
                return issue, lag
    raise AssertionError(f"no timing date found for {cp.legal_name} want_span={want_span}")


def build(rng: Random, ctx: EventContext) -> Event:
    cp = ctx.pools.non_card.pick(rng)
    draft, _ = make_invoice(rng, ctx.world, cp)
    want_span = ctx.ordinal % 2 == 0
    issue_date, lag = _plan_dates(rng, ctx.world, cp, draft.issue_date, want_span)
    invoice, decoys = make_invoice(
        rng,
        ctx.world,
        cp,
        allow_round_decoy=True,
        issue_date=issue_date,
        amount_minor=None,
    )
    value_date = add_business_days(invoice.due_date, lag)

    if cp.payment_method == "direct":
        leg = bank_leg(rng, cp, value_date=value_date, amount_minor=invoice.base_amount_minor)
    else:
        created_on = add_business_days(value_date, -rng.randint(LAG_MIN, LAG_MAX))
        leg = settlement_leg(
            rng,
            cp,
            invoice,
            settled_on=value_date,
            gross_minor=invoice.base_amount_minor,
            created_on=created_on,
        )

    observed = business_days_between(invoice.due_date, value_date)
    if not LAG_MIN <= observed <= LAG_MAX:
        raise AssertionError(f"timing lag {observed} bd outside {LAG_MIN}..{LAG_MAX}")
    if spans_non_business_day(invoice.due_date, value_date) != want_span:
        raise AssertionError("timing case lost its weekend/holiday span")

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="timing",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=value_date,
        invoices=[invoice],
        legs=[leg],
        decoys=decoys,
        notes=f"lag={observed}bd",
    )

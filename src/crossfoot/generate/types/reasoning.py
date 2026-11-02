"""`reasoning` -- 3% of events, three sub-types (spec 01 §4.8).

The only class *asserted unsolvable* by the shared `Thresholds`. Each sub-type is constructed to
fall outside the tolerance the matcher is told about, and then checked against that same
tolerance; a case that fails the check aborts generation rather than quietly weakening the
headline numbers. This, template D and the §7 decoys are the three places §13 is allowed to
claim the data is harder than the rules know.

  (a) `dba`      -- invoice to a subsidiary, payer is the parent from the hidden registry.
                    Asserted *below* `NAME_SIM_MIN` against both ERP names.
  (b) `short_pay` -- payment short by 10-40%, with the reason only in the PDF narrative, which
                    cites a credit note that is not in the dataset.
  (c) `rounding`  -- a consolidated payment off the exact sum by 1-5 minor units.

Session 1.3 resolution of §4.8(b): the spec writes X as "5-40% of gross" *and* asserts
`net/gross` outside `FEE_WINDOW`. With `FEE_WINDOW = (0.90, 1.02)` a 5% short-pay gives 0.95,
which is inside the window -- the two halves cannot both hold. The assertion wins, because being
outside the fee window is the whole reason the class exists, so X is drawn from
`SHORT_PAY_BP` = 10.5-40%.
"""

from random import Random
from typing import Final

from crossfoot.config import Thresholds
from crossfoot.generate.records import Event
from crossfoot.generate.types.common import (
    EventContext,
    assert_on_time,
    assert_unresolvable_name,
    group_paid_on,
    make_invoice,
    make_invoice_group,
    on_time_paid_on,
    parent_of,
    payment_leg,
    split_exact,
    surface_as_written,
    value_date_for,
)
from crossfoot.generate.world import ParentGroup
from crossfoot.money import ratio

# 1050bp..4000bp. The floor is 1050, not 1000, so `net/gross` clears FEE_WINDOW's 0.90 edge
# with room to spare rather than landing exactly on an open bound.
SHORT_PAY_BP: Final = (1_050, 4_000)
ROUNDING_RESIDUAL: Final = (1, 5)
ROUNDING_INVOICES: Final = (2, 3, 4)
DBA_ATTEMPTS: Final = 24


def _pick_parent(rng: Random, ctx: EventContext) -> tuple[ParentGroup, int]:
    """A hidden parent group and one of its members, whose names are far enough apart.

    Parent names are built from the same wordlists as counterparty names, so a group can happen
    to share a stem with its member (`ACME WIDGETS LTD` under `ACME CASTINGS GROUP PLC`). Those
    draws are rejected rather than accepted and asserted away.
    """
    for _ in range(DBA_ATTEMPTS):
        cp = ctx.pools.dba.pick(rng)
        group = parent_of(ctx.world, cp.id)
        if group is None:
            continue
        try:
            assert_unresolvable_name(cp, group.parent_name)
        except AssertionError:
            continue
        return group, cp.id
    raise AssertionError("no parent/subsidiary pair is far enough apart for reasoning(a)")


def _build_dba(rng: Random, ctx: EventContext) -> Event:
    group, counterparty_id = _pick_parent(rng, ctx)
    cp = ctx.world.by_id(counterparty_id)
    invoice, _ = make_invoice(rng, ctx.world, cp)
    paid_on = on_time_paid_on(rng, invoice)
    value_date = value_date_for(rng, paid_on)

    leg = payment_leg(
        rng,
        cp,
        invoice,
        value_date=value_date,
        amount_minor=invoice.base_amount_minor,
        payer_surface=group.parent_name,
        preserve_name=True,
    )
    assert_on_time(invoice, value_date, "reasoning(a)")
    assert_unresolvable_name(cp, surface_as_written(leg, invoice))

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="reasoning",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=[invoice],
        legs=[leg],
        reasoning_subtype="dba",
        notes=f"payer={group.parent_name} is the parent of {cp.legal_name}",
    )


def _build_short_pay(rng: Random, ctx: EventContext) -> Event:
    cp = ctx.pools.non_card.pick(rng)
    invoice, _ = make_invoice(rng, ctx.world, cp)
    paid_on = on_time_paid_on(rng, invoice)
    value_date = value_date_for(rng, paid_on)

    gross = invoice.base_amount_minor
    withheld = max(1, gross * rng.randint(*SHORT_PAY_BP) // 10_000)
    net = gross - withheld
    if net <= 0:
        raise AssertionError(f"short_pay withheld {withheld} of {gross}, leaving nothing")

    low, high = Thresholds.FEE_WINDOW
    observed = ratio(net, gross)
    if low < observed < high:
        raise AssertionError(f"short_pay net/gross {observed} is inside FEE_WINDOW {low}..{high}")

    leg = payment_leg(rng, cp, invoice, value_date=value_date, amount_minor=net)
    assert_on_time(invoice, value_date, "reasoning(b)")

    # A 2025 credit note cannot exist: `make_credit_note` clamps every note to the invoice
    # window, so the ref the narrative cites is unresolvable by construction (§4.6).
    cited = f"CN-{ctx.world.invoice_end.year + 1}-{ctx.sub_ordinal + 1:04d}"

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="reasoning",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=[invoice],
        legs=[leg],
        has_pdf=True,
        reasoning_subtype="short_pay",
        notes=f"cited {cited} not in dataset; withheld {withheld} of {gross}",
    )


def _build_rounding(rng: Random, ctx: EventContext) -> Event:
    cp = ctx.pools.non_card.pick(rng)
    parts = rng.choice(ROUNDING_INVOICES)
    invoices = make_invoice_group(rng, ctx.world, cp, parts)
    total = sum(invoice.base_amount_minor for invoice in invoices)
    paid_on = group_paid_on(rng, invoices)
    value_date = value_date_for(rng, paid_on)

    size = rng.randint(*ROUNDING_RESIDUAL)
    residual = size if rng.randrange(2) else -size
    paid = total + residual

    leg = payment_leg(rng, cp, invoices[0], value_date=value_date, amount_minor=paid)
    assert_on_time(min(invoices, key=lambda item: item.due_date), value_date, "reasoning(c)")

    observed = paid - total
    if not ROUNDING_RESIDUAL[0] <= abs(observed) <= ROUNDING_RESIDUAL[1]:
        raise AssertionError(f"rounding residual {observed} outside {ROUNDING_RESIDUAL}")
    if abs(observed) <= Thresholds.EXACT_SUM_TOL:
        raise AssertionError(f"rounding residual {observed} is within EXACT_SUM_TOL")

    # The PDF's per-line amounts carry the same pennies, so the advice is internally consistent
    # and only disagrees with the ERP (§4.8c). Spread over the lines, summing to the residual.
    spread = _spread_residual(rng, observed, parts)

    return Event(
        index=ctx.index,
        ordinal=ctx.ordinal,
        event_id=ctx.event_id,
        match_type="reasoning",
        counterparty_id=cp.id,
        payment_method=cp.payment_method,
        paid_on=paid_on,
        invoices=invoices,
        legs=[leg],
        has_pdf=True,
        reasoning_subtype="rounding",
        notes=f"residual={observed} spread={','.join(str(value) for value in spread)}",
    )


def _spread_residual(rng: Random, residual: int, parts: int) -> list[int]:
    """Per-line pennies summing exactly to `residual`, sign preserved."""
    size = abs(residual)
    sign = 1 if residual > 0 else -1
    if size >= parts:
        return [sign * value for value in split_exact(rng, size, parts)]
    # Fewer pennies than lines: give one penny each to a deterministic subset.
    out = [0] * parts
    positions = list(range(parts))
    rng.shuffle(positions)
    for position in positions[:size]:
        out[position] = sign
    return out


BUILDERS = {
    "dba": _build_dba,
    "short_pay": _build_short_pay,
    "rounding": _build_rounding,
}


def build(rng: Random, ctx: EventContext) -> Event:
    subtype = ctx.reasoning_subtype
    if subtype is None:
        raise ValueError("a reasoning slot must carry its sub-type")
    builder = BUILDERS.get(subtype)
    if builder is None:
        raise ValueError(f"unknown reasoning sub-type {subtype!r}")
    event = builder(rng, ctx)
    if event.legs and event.legs[0].kind == "settlement" and event.legs[0].fee_minor:
        raise AssertionError("a reasoning event must not also be a fee case")
    return event

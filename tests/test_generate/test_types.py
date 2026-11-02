"""Each discrepancy class's defining property, over a generated world (spec 01 §4).

These read the in-memory `Event` objects rather than the written files; `test_validate.py`
covers the same ground from disk. Both matter: a builder can be right while an emitter drops the
record, and an emitter can be right about records a builder built wrongly.
"""

from collections import Counter
from decimal import Decimal

import pytest

from crossfoot.config import Thresholds
from crossfoot.generate.calendars import add_business_days, business_days_between
from crossfoot.generate.dataset import Dataset, generate
from crossfoot.generate.records import Event
from crossfoot.generate.types.reasoning import SHORT_PAY_BP
from crossfoot.money import ratio
from crossfoot.names import surface_similarity

EVENTS = 600
SEED = 11


@pytest.fixture(scope="module")
def built() -> Dataset:
    return generate(SEED, "full", EVENTS)


def of_type(data: Dataset, match_type: str, subtype: str | None = None) -> list[Event]:
    return [
        event
        for event in data.events
        if event.match_type == match_type
        and (subtype is None or event.reasoning_subtype == subtype)
    ]


def paid(event: Event) -> int:
    return sum(leg.amount_minor for leg in event.legs)


def invoiced(event: Event) -> int:
    return sum(invoice.base_amount_minor for invoice in event.invoices)


def test_every_type_is_represented(built: Dataset) -> None:
    observed = Counter(event.match_type for event in built.events)
    assert set(observed) == set(built.type_counts)
    assert all(count > 0 for count in observed.values())


def test_name_variants_are_drifted_but_resolvable(built: Dataset) -> None:
    events = of_type(built, "name")
    assert events
    for event in events:
        cp = built.world.by_id(event.counterparty_id)
        surface = event.legs[0].payer_surface
        assert surface_similarity((cp.legal_name, cp.trading_name), surface) >= (
            Thresholds.NAME_SIM_MIN
        )
        assert paid(event) == invoiced(event)


def test_partial_instalments_sum_exactly(built: Dataset) -> None:
    events = of_type(built, "partial")
    assert events
    for event in events:
        assert len(event.invoices) == 1
        assert 2 <= len(event.legs) <= 4
        assert paid(event) == invoiced(event)
        assert event.has_pdf


def test_consolidated_nets_the_credit_note(built: Dataset) -> None:
    events = of_type(built, "consolidated")
    assert events
    with_note = 0
    for event in events:
        offset = sum(note.base_amount_minor for note in event.credit_notes)
        assert len(event.legs) == 1
        assert paid(event) == invoiced(event) - offset
        # The group-size cap: never more than four source ids (spec 01 §4.6).
        assert len(event.invoices) + len(event.credit_notes) <= 4
        assert event.has_pdf
        if event.credit_notes:
            with_note += 1
            note = event.credit_notes[0]
            assert note.issued_on < event.paid_on
            assert note.issued_on <= built.world.invoice_end
    assert with_note, "the 40% credit-note variant never fired"


def test_fx_events_are_foreign_and_card_free(built: Dataset) -> None:
    events = of_type(built, "fx")
    assert events
    for event in events:
        cp = built.world.by_id(event.counterparty_id)
        assert cp.currency in {"USD", "EUR"}
        assert not cp.is_card
        invoice = event.invoices[0]
        assert invoice.fx_rate_ppm is not None
        assert invoice.fx_rate_date == invoice.issue_date
        leg = event.legs[0]
        if leg.kind == "settlement":
            # Settled in the invoice currency, with the settlement-date rate recorded.
            assert leg.currency == cp.currency
            assert leg.fx_rate_ppm == built.world.fx[leg.value_date][cp.currency]
        else:
            assert leg.currency == "GBP"


def test_reasoning_dba_is_out_of_fuzzy_reach(built: Dataset) -> None:
    events = of_type(built, "reasoning", "dba")
    assert events
    for event in events:
        cp = built.world.by_id(event.counterparty_id)
        surface = event.legs[0].payer_surface
        score = surface_similarity((cp.legal_name, cp.trading_name), surface)
        assert score < Thresholds.NAME_SIM_MIN, (surface, cp.legal_name, score)
        assert paid(event) == invoiced(event)


def test_reasoning_short_pay_falls_outside_the_fee_window(built: Dataset) -> None:
    events = of_type(built, "reasoning", "short_pay")
    assert events
    low, high = Thresholds.FEE_WINDOW
    for event in events:
        observed = ratio(paid(event), invoiced(event))
        assert not low < observed < high, observed
        # The written range is 10.5%-40% short, which is what keeps it outside the window.
        withheld = invoiced(event) - paid(event)
        share = ratio(withheld, invoiced(event))
        assert Decimal(SHORT_PAY_BP[0]) / 10_000 <= share <= Decimal(SHORT_PAY_BP[1]) / 10_000
        assert event.has_pdf
        assert "CN-2025-" in str(event.notes)


def test_reasoning_rounding_residual_is_small_but_not_zero(built: Dataset) -> None:
    events = of_type(built, "reasoning", "rounding")
    assert events
    for event in events:
        residual = paid(event) - invoiced(event)
        assert 1 <= abs(residual) <= 5, residual
        assert abs(residual) > Thresholds.EXACT_SUM_TOL
        assert event.has_pdf


def test_duplicates_copy_the_money_not_the_identity(built: Dataset) -> None:
    events = of_type(built, "duplicate")
    assert events
    for event in events:
        assert len(event.legs) == 1 and len(event.duplicate_legs) == 1
        canonical, copy = event.legs[0], event.duplicate_legs[0]
        assert copy.amount_minor == canonical.amount_minor
        assert copy.value_date == canonical.value_date
        assert copy.record_id != canonical.record_id
        # "The earlier id is canonical" (spec 01 §4.9).
        assert canonical.record_id < copy.record_id
        assert not event.has_pdf


def test_no_match_orphans_split_evenly(built: Dataset) -> None:
    events = of_type(built, "no_match")
    assert events
    kinds = Counter(str(event.no_match_kind) for event in events)
    assert set(kinds) == {"open_invoice", "unknown_receipt"}
    assert abs(kinds["open_invoice"] - kinds["unknown_receipt"]) <= 1
    for event in events:
        assert not event.has_pdf
        if event.no_match_kind == "open_invoice":
            assert event.invoices and not event.legs
        else:
            assert event.legs and not event.invoices


def test_clean_and_timing_stay_on_opposite_sides_of_the_boundary(built: Dataset) -> None:
    """The sharp edge the whole dataset turns on (spec 01 §4.1, §4.3)."""
    for event in of_type(built, "clean"):
        invoice = event.invoices[0]
        limit = add_business_days(invoice.due_date, 1)
        assert all(leg.value_date <= limit for leg in event.legs)
    for event in of_type(built, "timing"):
        invoice = event.invoices[0]
        for leg in event.legs:
            lag = business_days_between(invoice.due_date, leg.value_date)
            assert 2 <= lag <= 5, lag


def test_card_product_implies_a_fee_event(built: Dataset) -> None:
    """A card line is a `fee` line and nothing else (spec 01 §4.2)."""
    for event in built.events:
        for leg in event.all_legs:
            if leg.product == "card":
                assert event.match_type == "fee", event.event_id
            if leg.fee_minor:
                assert event.match_type == "fee", event.event_id


def test_processor_events_emit_no_per_invoice_bank_line(built: Dataset) -> None:
    """The bank sees one TFR per batch, never the individual payment (spec 01 §3.2)."""
    for event in built.events:
        if event.payment_method != "processor":
            continue
        assert all(leg.kind == "settlement" for leg in event.all_legs), event.event_id

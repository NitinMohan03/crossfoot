"""The canonical envelope holds its three conventions, for all four sources.

Each source gets a record here even though only the ERP has a server yet: the point of the
schema is that 2.2-2.4 add a `detail` variant and nothing else, and a test that only ever built
ERP records would not notice if that stopped being true.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from crossfoot.config import Thresholds
from crossfoot.models.canonical import (
    AdviceLine,
    BankDetail,
    CanonicalRecord,
    ErpDetail,
    Money,
    PartyRef,
    ProcessorDetail,
    RemittanceDetail,
)

GBP = Money(minor=441700, currency="GBP", base_minor=441700)
ACME = PartyRef(
    surface="ACME WIDGETS LTD",
    normalised="ACME WIDGETS",
    counterparty_id=7,
    method="exact",
    similarity=100,
)


def invoice(**over: object) -> CanonicalRecord:
    fields: dict[str, object] = {
        "record_id": "erp:INV-2024-0891",
        "source": "erp",
        "role": "obligation",
        "amount": GBP,
        "party": ACME,
        "record_date": date(2024, 3, 1),
        "due_date": date(2024, 3, 31),
        "reference": "INV-2024-0891",
        "detail": ErpDetail(document="invoice", counterparty_id=7, terms_days=30),
    }
    return CanonicalRecord(**(fields | over))  # type: ignore[arg-type]


def test_an_erp_invoice_round_trips_through_json() -> None:
    record = invoice()
    again = CanonicalRecord.model_validate_json(record.model_dump_json())
    assert again == record
    assert again.detail.kind == "erp"


def test_the_discriminator_picks_the_variant_back_out_of_json() -> None:
    payment = CanonicalRecord(
        record_id="proc:stl_7f3a12bc45de",
        source="processor",
        role="payment",
        amount=GBP,
        party=ACME,
        record_date=date(2024, 3, 14),
        event_time=datetime(2024, 3, 13, 16, 42, 7, tzinfo=UTC),
        event_time_source="2024-03-13T16:42:07Z",
        detail=ProcessorDetail(
            batch_id="bat_2024-03-14_02", product="card", fee_minor=6444, net_minor=435256
        ),
        raw='{"id":"proc:stl_7f3a12bc45de"}',
    )
    again = CanonicalRecord.model_validate_json(payment.model_dump_json())
    assert isinstance(again.detail, ProcessorDetail)
    assert again.detail.net_minor == 435256
    assert again.raw == payment.raw


def test_a_bank_line_and_an_advice_use_the_same_envelope() -> None:
    bank = CanonicalRecord(
        record_id="bank:2024-03-15:000004",
        source="bank",
        role="payment",
        amount=GBP,
        party=PartyRef(surface="ACME WIDGETS LT", normalised="ACME WIDGETS LT"),
        record_date=date(2024, 3, 15),
        event_time=datetime(2024, 3, 31, 0, 30, tzinfo=UTC),
        event_time_source="2024-03-31T01:30:00+01:00",
        free_text="ACME WIDGETS LT INV-2024-0891",
        reference_surface="INV-2024-0891",
        detail=BankDetail(txn_type="FPI", balance_minor=1234567),
    )
    advice = CanonicalRecord(
        record_id="remit:RA-2024-0088",
        source="remittance",
        role="advice",
        amount=GBP,
        party=ACME,
        record_date=date(2024, 3, 15),
        detail=RemittanceDetail(
            payment_ref="FP-2024-0088",
            lines=(AdviceLine(invoice_ref="INV-2024-0891", amount_minor=441700),),
            narrative="Short paid, see CN-2025-0031 next month.",
        ),
    )
    assert bank.party.counterparty_id is None
    assert bank.party.method == "unresolved"
    assert isinstance(advice.detail, RemittanceDetail)
    # The advice total lives in `amount`, not in a second field on the detail.
    assert advice.amount.minor == sum(line.amount_minor for line in advice.detail.lines)


def test_a_credit_note_is_negative_in_both_currencies() -> None:
    note = invoice(
        record_id="erp:CN-2024-0007",
        amount=Money(minor=-12500, currency="GBP", base_minor=-12500),
        reference="CN-2024-0007",
        detail=ErpDetail(document="credit_note", counterparty_id=7),
        due_date=None,
    )
    assert note.amount.minor < 0
    assert note.amount.base_minor < 0


def test_a_foreign_amount_carries_its_rate_and_its_gbp_view() -> None:
    usd = Money(
        minor=500000,
        currency="USD",
        base_minor=393701,
        fx_rate_ppm=1_270_000,
        fx_rate_date=date(2024, 3, 1),
    )
    assert usd.base_minor != usd.minor


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_minor": 1}, "base_minor must equal minor"),
        (
            {"base_minor": 441700, "fx_rate_ppm": 1_270_000, "fx_rate_date": date(2024, 3, 1)},
            "carries no fx rate",
        ),
    ],
)
def test_a_gbp_amount_refuses_a_second_opinion(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Money(minor=441700, currency="GBP", **kwargs)  # type: ignore[arg-type]


def test_a_rate_without_its_date_is_refused() -> None:
    with pytest.raises(ValidationError, match="go together or not at all"):
        Money(minor=1, currency="USD", base_minor=1, fx_rate_ppm=1_270_000)


def test_a_sign_disagreement_between_the_two_currencies_is_refused() -> None:
    with pytest.raises(ValidationError, match="disagree about sign"):
        Money(
            minor=-100,
            currency="USD",
            base_minor=79,
            fx_rate_ppm=1_270_000,
            fx_rate_date=date(2024, 3, 1),
        )


def test_a_currency_that_is_not_three_letters_is_refused() -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        Money(minor=1, currency="GBPX", base_minor=1)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValidationError, match="tz-aware UTC"):
        invoice(event_time=datetime(2024, 3, 1, 9, 0))


def test_a_local_offset_timestamp_is_refused() -> None:
    bst = timezone(timedelta(hours=1))
    with pytest.raises(ValidationError, match="must be UTC"):
        invoice(event_time=datetime(2024, 3, 31, 1, 30, tzinfo=bst))


def test_the_original_timestamp_text_needs_a_timestamp_to_belong_to() -> None:
    with pytest.raises(ValidationError, match="evidence of nothing"):
        invoice(event_time_source="2024-03-31T01:30:00+01:00")


def test_an_id_from_the_wrong_source_is_refused() -> None:
    with pytest.raises(ValidationError, match="starts with 'erp:'"):
        invoice(record_id="bank:2024-03-15:000004")


def test_a_detail_from_the_wrong_source_is_refused() -> None:
    with pytest.raises(ValidationError, match="carries bank detail"):
        invoice(detail=BankDetail(txn_type="FPI"))


def test_only_an_obligation_has_a_due_date() -> None:
    with pytest.raises(ValidationError, match="only an obligation has a due date"):
        CanonicalRecord(
            record_id="bank:2024-03-15:000004",
            source="bank",
            role="payment",
            amount=GBP,
            party=ACME,
            record_date=date(2024, 3, 15),
            due_date=date(2024, 3, 31),
            detail=BankDetail(txn_type="FPI"),
        )


def test_a_settlement_whose_fee_and_net_miss_the_gross_is_refused() -> None:
    with pytest.raises(ValidationError, match="does not equal gross"):
        CanonicalRecord(
            record_id="proc:stl_7f3a12bc45de",
            source="processor",
            role="payment",
            amount=GBP,
            party=ACME,
            record_date=date(2024, 3, 14),
            detail=ProcessorDetail(
                batch_id="bat_2024-03-14_02", product="card", fee_minor=1, net_minor=1
            ),
        )


def test_a_pay_by_bank_line_carries_no_fee() -> None:
    with pytest.raises(ValidationError, match="carry no fee"):
        ProcessorDetail(
            batch_id="bat_2024-03-14_02", product="pay_by_bank", fee_minor=1, net_minor=2
        )


def test_normalised_must_be_what_names_normalise_produces() -> None:
    with pytest.raises(ValidationError, match=r"names\.normalise"):
        PartyRef(surface="Acme Widgets Ltd", normalised="acme widgets ltd")


def test_a_resolution_and_its_id_must_agree_that_it_happened() -> None:
    with pytest.raises(ValidationError, match="a resolved party needs an id"):
        PartyRef(surface="ACME WIDGETS LTD", normalised="ACME WIDGETS", method="alias")
    with pytest.raises(ValidationError, match="a resolved party needs an id"):
        PartyRef(surface="ACME WIDGETS LTD", normalised="ACME WIDGETS", counterparty_id=7)


def test_an_exact_resolution_claiming_partial_similarity_is_refused() -> None:
    with pytest.raises(ValidationError, match="similarity must be 100"):
        PartyRef(
            surface="ACME WIDGETS LTD",
            normalised="ACME WIDGETS",
            counterparty_id=7,
            method="alias",
            similarity=99,
        )


def test_a_fuzzy_resolution_below_the_shared_threshold_is_refused() -> None:
    with pytest.raises(ValidationError, match="below NAME_SIM_MIN"):
        PartyRef(
            surface="ACME WIDGETS LTD",
            normalised="ACME WIDGETS",
            counterparty_id=7,
            method="fuzzy",
            similarity=Thresholds.NAME_SIM_MIN - 1,
        )


def test_an_unknown_field_is_refused_rather_than_absorbed() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        invoice(status="paid")

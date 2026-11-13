"""Settlement lines land in the canonical schema, with the processor's conventions intact."""

import json
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

import pytest

from crossfoot.entities import resolve_description
from crossfoot.generate.fx import to_base_minor as generator_to_base_minor
from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError
from crossfoot.mcp.common.parties import StaticParties
from crossfoot.mcp.common.results import record_payload
from crossfoot.mcp.processor.canonical import (
    created,
    rate_ppm,
    settlement_record,
    to_base_minor,
)
from crossfoot.mcp.processor.wire import SettlementLine, parse_settlement
from crossfoot.models.canonical import CanonicalRecord, PartyRef
from crossfoot.names import normalise
from tests.test_mcp.processor.conftest import (
    BY_CURRENCY,
    BY_PRODUCT,
    FIRST_ID,
    SETTLEMENTS,
    USD_ID,
    WITH_REFERENCE,
    raw_lines,
)

RAW = {json.loads(raw)["id"]: raw for raw in raw_lines("settlements.jsonl")}

#: How entity resolution reads the 552 payer names with an empty registry. Pinned so a change to
#: the resolver or to the slice shows up here; `test_processor_payers.py` grades them.
PARTY_METHODS = {"exact": 405, "normalised": 114, "fuzzy": 11, "unresolved": 22}


def nobody(name: str) -> PartyRef:
    return PartyRef(surface=name, normalised=normalise(name))


def record_from(raw: str) -> CanonicalRecord:
    line = parse_settlement(raw)
    return settlement_record(line, nobody(line.payer_name), raw)


def edited(line_id: str, **changes: Any) -> str:
    record = json.loads(RAW[line_id])
    record.update(changes)
    return json.dumps(record)


def malformed(raw: str) -> ToolError:
    with pytest.raises(ToolError) as caught:
        record_from(raw)
    assert caught.value.code == MALFORMED_SOURCE
    return caught.value


@pytest.fixture(scope="module")
def lines() -> list[SettlementLine]:
    return [parse_settlement(raw) for raw in raw_lines("settlements.jsonl")]


@pytest.fixture(scope="module")
def records(lines: list[SettlementLine], static_parties: StaticParties) -> list[CanonicalRecord]:
    index, registry = static_parties.name_index(), static_parties.registry()
    return [
        settlement_record(line, resolve_description(line.payer_name, index, registry), RAW[line.id])
        for line in lines
    ]


# -- the whole slice -------------------------------------------------------------------------


def test_every_line_becomes_a_processor_payment_with_its_own_id(
    records: list[CanonicalRecord], manifest_proc_ids: set[str]
) -> None:
    assert len(records) == SETTLEMENTS
    assert {(record.source, record.role) for record in records} == {("processor", "payment")}
    # Spec 01 §5.3 ids, unchanged: ground truth knows every one, and no other.
    assert {record.record_id for record in records} == manifest_proc_ids
    assert Counter(record.amount.currency for record in records) == BY_CURRENCY
    assert Counter(record.detail.product for record in records) == BY_PRODUCT  # type: ignore[union-attr]


def test_the_amount_is_the_gross_and_fee_plus_net_make_it(
    lines: list[SettlementLine], records: list[CanonicalRecord]
) -> None:
    for line, record in zip(lines, records, strict=True):
        detail = record.detail
        assert detail.kind == "processor"
        assert record.amount.minor == line.gross_minor > 0
        assert (detail.fee_minor, detail.net_minor) == (line.fee_minor, line.net_minor)
        assert detail.fee_minor + detail.net_minor == record.amount.minor
        assert detail.batch_id == line.batch_id
        # Only a card payment is charged a fee (spec 01 §3.3).
        assert (detail.fee_minor > 0) == (detail.product == "card")


def test_a_sterling_line_is_its_own_base_amount(records: list[CanonicalRecord]) -> None:
    sterling = [record for record in records if record.amount.currency == "GBP"]
    assert len(sterling) == BY_CURRENCY["GBP"]
    assert all(record.amount.base_minor == record.amount.minor for record in sterling)
    assert all(record.amount.fx_rate_ppm is None for record in sterling)


def test_a_foreign_line_is_converted_at_its_own_recorded_rate(
    lines: list[SettlementLine], records: list[CanonicalRecord]
) -> None:
    foreign = [(line, r) for line, r in zip(lines, records, strict=True) if line.fx_rate]
    assert len(foreign) == BY_CURRENCY["USD"] + BY_CURRENCY["EUR"]
    for line, record in foreign:
        money = record.amount
        assert line.fx_rate is not None
        assert money.fx_rate_ppm is not None
        # The text and the integer are the same six digits: nothing was rounded on the way.
        assert (
            f"{money.fx_rate_ppm // 1_000_000}.{money.fx_rate_ppm % 1_000_000:06d}" == line.fx_rate
        )
        # The settlement-date rate (spec 01 §4.7), and the generator's own rounding.
        assert money.fx_rate_date == line.settled_on == record.record_date
        assert money.base_minor == generator_to_base_minor(money.minor, money.fx_rate_ppm)
        assert 0 < money.base_minor < money.minor


def test_one_foreign_line_worked_by_hand() -> None:
    record = record_from(RAW[USD_ID])
    # 34.54 USD at 1.333209 USD per GBP is 25.9074... GBP.
    assert (record.amount.minor, record.amount.currency) == (3454, "USD")
    assert (record.amount.fx_rate_ppm, record.amount.fx_rate_date) == (1_333_209, date(2024, 8, 21))
    assert record.amount.base_minor == 2591
    assert to_base_minor(250, 1_000_000) == 250
    # Half a penny rounds up, as `crossfoot.money` rounds everywhere else.
    assert to_base_minor(1, 2_000_000) == 1
    assert to_base_minor(3, 2_000_000) == 2


def test_settled_on_stays_a_date_and_created_at_becomes_utc(
    lines: list[SettlementLine], records: list[CanonicalRecord]
) -> None:
    for line, record in zip(lines, records, strict=True):
        assert record.record_date == line.settled_on
        assert type(record.record_date) is date
        assert record.event_time is not None
        assert record.event_time.utcoffset() == UTC.utcoffset(None)
        # The text the source wrote is kept beside the moment parsed out of it.
        assert record.event_time_source == line.created_at
        assert record.event_time.strftime("%Y-%m-%dT%H:%M:%SZ") == line.created_at
        assert record.due_date is None
    first = record_from(RAW[FIRST_ID])
    # Created on the 10th, settled on the 11th: two facts, neither derived from the other.
    assert first.event_time == datetime(2024, 1, 10, 19, 5, 39, tzinfo=UTC)
    assert first.record_date == date(2024, 1, 11)


def test_a_timestamp_with_another_offset_is_converted_and_its_text_kept() -> None:
    record = record_from(edited(FIRST_ID, created_at="2024-01-11T00:30:00+01:00"))
    assert record.event_time == datetime(2024, 1, 10, 23, 30, tzinfo=UTC)
    assert record.event_time_source == "2024-01-11T00:30:00+01:00"
    assert record.record_date == date(2024, 1, 11)


def test_the_payers_reference_is_kept_as_typed_and_claimed_as_nothing_more(
    lines: list[SettlementLine], records: list[CanonicalRecord]
) -> None:
    typed = [record for record in records if record.reference_surface is not None]
    assert len(typed) == WITH_REFERENCE
    for line, record in zip(lines, records, strict=True):
        assert record.reference_surface == line.payer_reference
        assert record.reference is None
        assert record.free_text is None
    # An invoice ref, a fragment of one, the payer's own PO number: all three occur.
    shapes = Counter(
        "invoice" if text.startswith("INV-") else "po" if text.startswith("PO") else "fragment"
        for text in (record.reference_surface for record in typed)
        if text is not None
    )
    assert set(shapes) == {"invoice", "po", "fragment"}
    assert record_from(edited(FIRST_ID, payer_reference="")).reference_surface is None


def test_raw_is_the_line_as_the_source_wrote_it(records: list[CanonicalRecord]) -> None:
    assert [record.raw for record in records] == raw_lines("settlements.jsonl")


def test_the_party_is_what_entity_resolution_made_of_the_payer_name(
    lines: list[SettlementLine], records: list[CanonicalRecord]
) -> None:
    assert Counter(record.party.method for record in records) == PARTY_METHODS
    for line, record in zip(lines, records, strict=True):
        assert record.party.surface == line.payer_name
    first = records[[record.record_id for record in records].index(FIRST_ID)]
    assert (first.party.surface, first.party.method) == ("KINGSMEAD TRADING LLP", "exact")
    assert first.party.counterparty_id is not None


def test_a_record_survives_the_trip_through_a_payload(records: list[CanonicalRecord]) -> None:
    for record in records[:25]:
        payload = record_payload(record)
        assert type(payload["amount"]["minor"]) is int
        assert type(payload["detail"]["fee_minor"]) is int
        assert CanonicalRecord.model_validate(json.loads(json.dumps(payload))) == record


# -- a line that contradicts itself ----------------------------------------------------------


def test_fee_plus_net_that_is_not_the_gross_is_malformed_source() -> None:
    error = malformed(edited(FIRST_ID, net_minor=285_265))
    assert "fee + net = 289374 does not equal gross 289375" in error.message
    assert error.extra == {"record": FIRST_ID}


def test_a_fee_on_a_pay_by_bank_line_is_malformed_source() -> None:
    error = malformed(edited(USD_ID, fee_minor=10, net_minor=3444))
    assert "pay_by_bank lines carry no fee" in error.message


@pytest.mark.parametrize(
    ("line_id", "changes", "expected"),
    [
        (FIRST_ID, {"fx_rate": "1.270000", "fx_rate_date": "2024-01-11"}, "carries an fx_rate"),
        (FIRST_ID, {"fx_rate_date": "2024-01-11"}, "carries no fx rate"),
        (USD_ID, {"fx_rate": None, "fx_rate_date": None}, "a USD line carries no fx_rate"),
        (USD_ID, {"fx_rate_date": None}, "go together"),
        (USD_ID, {"fx_rate": "about 1.3"}, "not a decimal number"),
        (USD_ID, {"fx_rate": "1.3332095"}, "at most six decimal places"),
        (USD_ID, {"fx_rate": "0"}, "positive rate"),
        (USD_ID, {"fx_rate": "-1.333209"}, "positive rate"),
        (USD_ID, {"fx_rate": "NaN"}, "positive rate"),
        (USD_ID, {"fx_rate": "Infinity"}, "positive rate"),
    ],
)
def test_a_rate_that_cannot_be_used_is_malformed_source(
    line_id: str, changes: dict[str, Any], expected: str
) -> None:
    error = malformed(edited(line_id, **changes))
    assert expected in error.message
    assert error.extra == {"record": line_id}


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("2024-01-10T19:05:39", "no UTC offset"),
        ("2024-01-10", "no UTC offset"),
        ("yesterday", "not an ISO-8601 timestamp"),
        ("", "not an ISO-8601 timestamp"),
    ],
)
def test_a_creation_time_that_is_not_a_moment_is_malformed_not_assumed_utc(
    created_at: str, expected: str
) -> None:
    assert expected in malformed(edited(FIRST_ID, created_at=created_at)).message


def test_the_helpers_agree_with_the_record() -> None:
    line = parse_settlement(RAW[USD_ID])
    assert rate_ppm(line) == 1_333_209
    assert rate_ppm(parse_settlement(RAW[FIRST_ID])) is None
    assert created(line) == datetime(2024, 8, 20, 9, 17, 37, tzinfo=UTC)

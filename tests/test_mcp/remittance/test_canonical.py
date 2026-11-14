"""Advices land in the canonical schema, with the remittance source's conventions intact.

The mapper is tested directly on the parsed developed advices: no service, no database. The
decisions under test are the ones `canonical.py` lists -- the derived id, the stated total and
the lines exactly as the payer wrote them, the currency symbol, the inferred sterling view of a
foreign advice, and a payment date that stays a date.
"""

from collections import Counter
from datetime import date
from typing import Any

import pytest

from crossfoot.generate.fx import to_base_minor as generator_to_base_minor
from crossfoot.generate.read import read_erp
from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError
from crossfoot.mcp.common.results import record_payload
from crossfoot.mcp.remittance.canonical import (
    CURRENCY_OF,
    advice_record,
    payment_ref_of,
    remittance_id,
)
from crossfoot.mcp.remittance.fx import Rates, parse_rates
from crossfoot.mcp.remittance.parse import SYMBOLS, Advice
from crossfoot.mcp.remittance.settings import FX_FILE
from crossfoot.models.canonical import CanonicalRecord, PartyRef, RemittanceDetail
from crossfoot.names import normalise
from tests.test_mcp.conftest import DEV_SLICE
from tests.test_mcp.remittance.conftest import ADVICES, DEVELOPED, FOREIGN_DEVELOPED

#: How entity resolution reads the 444 developed payer names with an empty registry. Pinned so
#: a change to the resolver or the slice shows here; `test_remittance_payers.py` grades them.
PARTY_METHODS = {"exact": 317, "normalised": 96, "fuzzy": 9, "unresolved": 22}

#: Developed advices by ground-truth class.
BY_CLASS = {
    "clean": 60,
    "consolidated": 94,
    "dba": 10,
    "fee": 34,
    "fx": 35,
    "name": 33,
    "partial": 88,
    "rounding": 29,
    "short_pay": 29,
    "timing": 32,
}

EUR_ID, EUR_REF = "remit:RA-2024-0027", "PAY-2024-0027"
USD_ID = "remit:RA-2024-0022"
ROUNDING_ID = "remit:RA-2024-0003"
DBA_ID = "remit:RA-2024-0065"


def nobody(name: str) -> PartyRef:
    return PartyRef(surface=name, normalised=normalise(name))


def kind_of(group: dict[str, Any]) -> str:
    return str(group.get("reasoning_subtype") or group["match_type"])


# -- the id ----------------------------------------------------------------------------------


def test_the_id_rule_holds_for_every_advice_ground_truth_lists(truth: list[dict[str, Any]]) -> None:
    # All 525, held-out layout included: this reads `remit.jsonl`, not a PDF.
    assert len(truth) == ADVICES
    for row in truth:
        assert remittance_id(row["payment_ref"]) == row["id"]
        assert payment_ref_of(row["id"]) == row["payment_ref"]
        assert payment_ref_of(row["id"].removeprefix("remit:")) == row["payment_ref"]


@pytest.mark.parametrize(
    "value",
    ["", "PAY-2024-0088", "remit:PAY-2024-0088", "remit:RA-2024-88", "RA-24-0088", "remit:RA-x"],
)
def test_what_is_not_a_remittance_id_is_not_turned_into_a_reference(value: str) -> None:
    assert payment_ref_of(value) is None


def test_a_reference_that_is_not_one_cannot_be_given_an_id() -> None:
    with pytest.raises(ValueError, match="not a payment reference"):
        remittance_id("RA-2024-0088")


def test_every_symbol_the_parser_accepts_has_a_currency() -> None:
    assert set(CURRENCY_OF) == set(SYMBOLS)
    assert sorted(CURRENCY_OF.values()) == ["EUR", "GBP", "USD"]


# -- every developed advice ------------------------------------------------------------------


def test_every_developed_advice_becomes_a_record_the_manifest_knows(
    records: dict[str, CanonicalRecord],
    developed: list[dict[str, Any]],
    group_of: dict[str, dict[str, Any]],
) -> None:
    assert len(records) == DEVELOPED
    assert set(records) == {row["id"] for row in developed}
    # Every one is evidence for exactly one ground-truth group (spec 01 §5.1).
    assert set(records) <= set(group_of)
    assert Counter(kind_of(group_of[record_id]) for record_id in records) == BY_CLASS


def test_the_envelope_says_what_the_advice_said(
    records: dict[str, CanonicalRecord], developed: list[dict[str, Any]]
) -> None:
    for row in developed:
        record = records[row["id"]]
        assert (record.source, record.role) == ("remittance", "advice")
        assert record.record_date == date.fromisoformat(row["paid_on"])
        assert record.amount.minor == row["total_minor"]
        assert record.reference == row["payment_ref"]
        assert record.free_text == row["narrative"]
        assert record.party.surface == row["payer"]
        # An advice records no moment, has no due date, and is not one line of text.
        assert record.event_time is None and record.event_time_source is None
        assert record.due_date is None and record.raw is None
        assert record.reference_surface is None
        detail = record.detail
        assert isinstance(detail, RemittanceDetail)
        assert detail.payment_ref == row["payment_ref"]
        assert detail.narrative == row["narrative"]
        assert [line.model_dump() for line in detail.lines] == [
            {key: line[key] for key in ("invoice_ref", "amount_minor", "note")}
            for line in row["lines"]
        ]


def test_parties_are_resolved_and_the_spread_is_pinned(
    records: dict[str, CanonicalRecord],
) -> None:
    assert Counter(record.party.method for record in records.values()) == PARTY_METHODS


def test_a_record_survives_the_trip_to_a_payload_and_back(
    records: dict[str, CanonicalRecord],
) -> None:
    for record_id in (EUR_ID, ROUNDING_ID, DBA_ID):
        payload = record_payload(records[record_id])
        assert CanonicalRecord.model_validate(payload) == records[record_id]
        assert isinstance(payload["amount"]["minor"], int)
        assert all(isinstance(line["amount_minor"], int) for line in payload["detail"]["lines"])
        # No layout id anywhere on the record: the PDF does not say, so neither does this.
        assert "template" not in str(payload).lower()


# -- what the payer wrote is not reconciled --------------------------------------------------


def test_a_rounding_advice_keeps_the_payers_pennies(
    records: dict[str, CanonicalRecord], group_of: dict[str, dict[str, Any]]
) -> None:
    invoices = read_erp(DEV_SLICE / "erp.sql").invoice_by_ref()
    record = records[ROUNDING_ID]
    assert isinstance(record.detail, RemittanceDetail)
    # The advice says 3471.35 and 2967.23; the ledger says 3471.32 and 2967.21.
    assert [line.amount_minor for line in record.detail.lines] == [347_135, 296_723]
    assert [invoices[line.invoice_ref].base_amount_minor for line in record.detail.lines] == [
        347_132,
        296_721,
    ]
    assert {line.note for line in record.detail.lines} == {"rounding adjustment"}
    assert record.amount.minor == 643_858

    differing = 0
    residuals: Counter[int] = Counter()
    for record_id, other in records.items():
        if kind_of(group_of[record_id]) != "rounding":
            continue
        assert isinstance(other.detail, RemittanceDetail)
        ledger = [invoices[line.invoice_ref].base_amount_minor for line in other.detail.lines]
        stated = [line.amount_minor for line in other.detail.lines]
        differing += stated != ledger
        residuals[sum(stated) - sum(ledger)] += 1
    # All 29: every one disagrees with the ledger by 1-5 pence, and still does after mapping.
    assert differing == BY_CLASS["rounding"]
    assert set(residuals) <= {-5, -4, -3, -2, -1, 1, 2, 3, 4, 5}
    assert sum(residuals.values()) == BY_CLASS["rounding"]


def test_a_short_payment_is_recorded_as_the_short_amount(
    records: dict[str, CanonicalRecord], group_of: dict[str, dict[str, Any]]
) -> None:
    invoices = read_erp(DEV_SLICE / "erp.sql").invoice_by_ref()
    short = [r for i, r in records.items() if kind_of(group_of[i]) == "short_pay"]
    assert len(short) == BY_CLASS["short_pay"]
    for record in short:
        assert isinstance(record.detail, RemittanceDetail)
        (line,) = record.detail.lines
        assert line.amount_minor < invoices[line.invoice_ref].base_amount_minor
        assert line.note == "part payment, see note"
        # The reason is in the paragraph, kept whole; it cites a credit note nobody has issued.
        assert record.free_text is not None and "CN-2025-" in record.free_text


def test_a_parent_company_advice_names_the_subsidiary_only_in_its_paragraph(
    records: dict[str, CanonicalRecord], group_of: dict[str, dict[str, Any]]
) -> None:
    record = records[DBA_ID]
    assert record.party.surface == "BRAMBLE PROPERTIES GROUP PLC"
    assert (record.party.method, record.party.counterparty_id) == ("unresolved", None)
    assert record.free_text == (
        "Group payment run. The trading entity invoiced is PRESTWICH SUPPLIES LIMITED."
    )
    # The mapper does not go looking in the paragraph: all ten stay unresolved.
    parents = [r for i, r in records.items() if kind_of(group_of[i]) == "dba"]
    assert len(parents) == BY_CLASS["dba"]
    assert {r.party.method for r in parents} == {"unresolved"}


# -- currency and the sterling view ----------------------------------------------------------


def test_a_sterling_advice_is_its_own_base_and_carries_no_rate(
    records: dict[str, CanonicalRecord],
) -> None:
    sterling = [r for r in records.values() if r.amount.currency == "GBP"]
    assert len(sterling) == DEVELOPED - sum(FOREIGN_DEVELOPED.values())
    for record in sterling:
        assert record.amount.base_minor == record.amount.minor
        assert record.amount.fx_rate_ppm is None and record.amount.fx_rate_date is None


def test_a_foreign_advice_is_valued_at_its_own_payment_date_and_says_so(
    records: dict[str, CanonicalRecord], rates: Rates, currency_of: dict[str, str]
) -> None:
    foreign = {i: r for i, r in records.items() if r.amount.currency != "GBP"}
    assert Counter(r.amount.currency for r in foreign.values()) == FOREIGN_DEVELOPED
    for record_id, record in foreign.items():
        amount = record.amount
        assert amount.currency == currency_of[record_id]
        assert amount.fx_rate_date == record.record_date
        assert amount.fx_rate_ppm == rates[(record.record_date, amount.currency)]
        # The same arithmetic the generator uses to put a foreign amount into sterling.
        assert amount.base_minor == generator_to_base_minor(amount.minor, amount.fx_rate_ppm)
        assert 0 < amount.base_minor < amount.minor


def test_two_foreign_advices_to_the_penny(records: dict[str, CanonicalRecord]) -> None:
    euro = records[EUR_ID].amount
    # EUR 3,629.36 at 1.148426 on 2024-02-16 is GBP 3,160.29 (3160.2907..., rounded half up).
    assert (euro.minor, euro.currency, euro.fx_rate_ppm) == (362_936, "EUR", 1_148_426)
    assert (euro.base_minor, euro.fx_rate_date) == (316_029, date(2024, 2, 16))
    dollar = records[USD_ID].amount
    assert (dollar.minor, dollar.currency, dollar.fx_rate_ppm) == (118_636, "USD", 1_310_674)
    assert (dollar.base_minor, dollar.fx_rate_date) == (90_515, date(2024, 2, 9))


def test_the_currency_and_the_rate_must_agree_about_being_foreign(
    parsed: dict[str, Advice],
) -> None:
    euro, sterling = parsed[EUR_REF], parsed["PAY-2024-0001"]
    with pytest.raises(ValueError, match="do not go together"):
        advice_record(euro, nobody(euro.payer), None)
    with pytest.raises(ValueError, match="do not go together"):
        advice_record(sterling, nobody(sterling.payer), 1_148_426)


# -- the rate table --------------------------------------------------------------------------


def test_the_rate_table_is_one_rate_per_currency_per_day(rates: Rates) -> None:
    assert len(rates) == 794
    days = sorted({day for day, _ in rates})
    assert (days[0], days[-1], len(days)) == (date(2024, 1, 1), date(2025, 1, 31), 397)
    assert {currency for _, currency in rates} == {"USD", "EUR"}
    assert rates[(date(2024, 1, 1), "USD")] == 1_270_000
    assert rates[(date(2024, 1, 2), "EUR")] == 1_164_116
    assert all(type(ppm) is int for ppm in rates.values())


def test_crlf_and_a_missing_final_newline_are_the_same_table() -> None:
    text = "date,USD,EUR\r\n2024-01-01,1.270000,1.160000\r\n2024-01-02,1.272579,1.164116"
    assert parse_rates(FX_FILE, text) == {
        (date(2024, 1, 1), "USD"): 1_270_000,
        (date(2024, 1, 1), "EUR"): 1_160_000,
        (date(2024, 1, 2), "USD"): 1_272_579,
        (date(2024, 1, 2), "EUR"): 1_164_116,
    }


@pytest.mark.parametrize(
    ("text", "says", "row"),
    [
        ("", "the rate table is empty", 0),
        ("day,USD,EUR\n", "the heading is not `date`", 0),
        ("date\n2024-01-01\n", "the heading is not `date`", 0),
        ("date,USD,usd\n", "not a three-letter code", 0),
        ("date,USD,USD\n", "appears twice", 0),
        ("date,USD,EUR\n2024-01-01,1.27\n", "expected 3 fields, found 2", 1),
        ("date,USD,EUR\n01/01/2024,1.27,1.16\n", "is not an ISO date", 1),
        ("date,USD,EUR\n2024-01-01,1.27,1.16\n2024-01-01,1.27,1.16\n", "appears twice", 2),
        ("date,USD,EUR\n2024-01-01,1.2700001,1.16\n", "at most six decimal places", 1),
        ("date,USD,EUR\n2024-01-01,n/a,1.16\n", "'n/a' is not a rate", 1),
        ("date,USD,EUR\n2024-01-01,0.000000,1.16\n", "not a positive rate", 1),
        ("date,USD,EUR\n2024-01-01,-1.27,1.16\n", "not a positive rate", 1),
    ],
)
def test_a_rate_table_that_is_not_one_is_refused_by_row(text: str, says: str, row: int) -> None:
    with pytest.raises(ToolError) as caught:
        parse_rates(FX_FILE, text)
    assert caught.value.code == MALFORMED_SOURCE
    assert says in caught.value.message
    assert caught.value.extra == {"file": FX_FILE, "row": row}

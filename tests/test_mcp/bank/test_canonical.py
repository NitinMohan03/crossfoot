"""A bank row becomes a canonical record, with every convention intact (plan §2.7).

The timezone test is the one the project uses this source for. The CSV's `Transaction Date` is
a calendar date and must come through untouched; the sidecar's `posted_at` is a moment written
in `Europe/London` local time and must come through as UTC with the original text beside it. The
slice straddles both clock changes of 2024, so both offsets are exercised by real lines.
"""

import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from crossfoot.mcp.bank.canonical import bank_record, party_for, reference_fragment, unresolved
from crossfoot.mcp.bank.parse import BankRow, PostedAt
from crossfoot.mcp.bank.tools import BankService
from crossfoot.mcp.common.results import record_payload
from crossfoot.models.canonical import BankDetail, CanonicalRecord, PartyRef
from tests.test_mcp.bank.conftest import BY_TYPE, LINES, PAYER_REFERENCES, PAYOUT_REFERENCES

#: The last working day before the clocks went forward (31 March 2024), posted late.
WINTER_LINE = "bank:2024-03-28:000091"
#: The first line after, posted two minutes past midnight local time.
SUMMER_LINE = "bank:2024-04-02:000003"


def by_id(rows: tuple[BankRow, ...], line_id: str) -> BankRow:
    return next(row for row in rows if row.line_id == line_id)


def record(rows: tuple[BankRow, ...], posted: dict[str, PostedAt], line_id: str) -> CanonicalRecord:
    row = by_id(rows, line_id)
    return bank_record(row, unresolved(row.description), posted[line_id])


# -- timezones -------------------------------------------------------------------------------


def test_a_winter_posting_is_already_utc_and_says_so(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    line = record(rows, posted, WINTER_LINE)
    assert line.event_time_source == "2024-03-28T23:00:00+00:00"
    assert line.event_time == datetime(2024, 3, 28, 23, 0, tzinfo=UTC)
    assert line.record_date == date(2024, 3, 28)


def test_a_summer_posting_moves_an_hour_and_the_value_date_does_not_move_at_all(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    line = record(rows, posted, SUMMER_LINE)
    # The source's own text, offset included, is the evidence of what was converted.
    assert line.event_time_source == "2024-04-02T00:02:00+01:00"
    assert line.event_time == datetime(2024, 4, 1, 23, 2, tzinfo=UTC)
    # The same instant, just not the same wall clock.
    assert line.event_time == datetime.fromisoformat(line.event_time_source)
    # And here is why a calendar date is never converted: in UTC this line was posted on the
    # 1st, and its value date is -- and stays -- the 2nd.
    assert line.event_time.date() == date(2024, 4, 1)
    assert line.record_date == date(2024, 4, 2)


def test_the_two_sides_of_the_clock_change_differ_by_exactly_the_hour(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    winter, summer = record(rows, posted, WINTER_LINE), record(rows, posted, SUMMER_LINE)
    assert winter.event_time is not None
    assert summer.event_time is not None
    local_gap = datetime(2024, 4, 2, 0, 2) - datetime(2024, 3, 28, 23, 0)
    assert summer.event_time - winter.event_time == local_gap - timedelta(hours=1)


def test_every_line_is_utc_and_no_value_date_was_shifted(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    shifted = 0
    for row in rows:
        line = bank_record(row, unresolved(row.description), posted[row.line_id])
        assert line.event_time is not None
        assert line.event_time_source is not None
        assert line.event_time.utcoffset() == timedelta(0)
        assert line.record_date == row.value_date
        # The local date in the source text is the value date, on every line.
        assert line.event_time_source[:10] == row.value_date.isoformat()
        shifted += line.event_time.date() != line.record_date
    # 30 lines were posted in the first hour of a summer day: in UTC, the day before. Had the
    # value date been converted along with them, 30 payments would sit on the wrong day.
    assert shifted == 30


def test_the_envelope_refuses_a_local_time_where_utc_belongs(rows: tuple[BankRow, ...]) -> None:
    row = by_id(rows, SUMMER_LINE)
    local = datetime(2024, 4, 2, 0, 2, tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises(ValidationError, match="must be UTC"):
        bank_record(row, unresolved(row.description), PostedAt(local, local.isoformat()))


def test_a_line_the_sidecar_lacks_has_no_event_time_and_no_invented_one(
    rows: tuple[BankRow, ...],
) -> None:
    line = bank_record(rows[0], unresolved(rows[0].description), None)
    assert line.event_time is None
    assert line.event_time_source is None
    assert line.record_date == rows[0].value_date


# -- money, sign, shape ----------------------------------------------------------------------


def test_money_in_is_positive_money_out_negative_and_all_of_it_sterling(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    signs: Counter[tuple[str, bool]] = Counter()
    for row in rows:
        line = bank_record(row, unresolved(row.description), posted[row.line_id])
        assert line.amount.currency == "GBP"
        assert line.amount.base_minor == line.amount.minor
        assert line.amount.fx_rate_ppm is None
        assert type(line.amount.minor) is int
        signs[(row.txn_type, line.amount.minor > 0)] += 1
    assert signs == {
        ("FPI", True): BY_TYPE["FPI"],
        ("BGC", True): BY_TYPE["BGC"],
        ("TFR", True): BY_TYPE["TFR"],
        ("FPO", False): BY_TYPE["FPO"],
    }


def test_a_refund_is_a_negative_payment(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    line = record(rows, posted, "bank:2024-02-22:000047")
    assert line.free_text == "MERIDIAN PACKAGING LTD"
    assert line.amount.minor == -17_150
    assert isinstance(line.detail, BankDetail)
    assert line.detail.txn_type == "FPO"


def test_the_record_is_a_bank_payment_with_the_manifests_id(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    line = record(rows, posted, "bank:2024-03-01:000001")
    assert (line.record_id, line.source, line.role) == ("bank:2024-03-01:000001", "bank", "payment")
    assert line.due_date is None
    assert line.free_text == "MERIDIAN AEROSPACE INV0014"
    assert line.raw == "01/03/2024,FPI,MERIDIAN AEROSPACE INV0014,,2492.30,527768.53"
    assert line.detail == BankDetail(txn_type="FPI", balance_minor=52_776_853)
    # The bank has no structured reference column, so the structured field stays empty.
    assert line.reference is None
    assert line.reference_surface == "INV0014"


def test_a_record_survives_the_trip_to_a_payload_and_back(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    for line_id in ("bank:2024-03-01:000001", SUMMER_LINE, "bank:2024-02-22:000047"):
        line = record(rows, posted, line_id)
        payload: dict[str, Any] = record_payload(line)
        assert isinstance(payload["amount"]["minor"], int)
        assert CanonicalRecord.model_validate(payload) == line


# -- the typed reference ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "fragment"),
    [
        ("BRAMBLE DESIGN LTD INV-2024-0577", "INV-2024-0577"),
        ("CARRICK FABRICATION INV0517", "INV0517"),
        ("EAZTGATE HOLDINGR LTD 0600", "0600"),
        ("CARRICK FABRICATION PO88766", "PO88766"),
        ("PAYCO SETTLE BAT_2024-02-29_02", "BAT_2024-02-29_02"),
        ("KINGSMEAD HOLDINGS LIMITED", None),
        ("EASTGATE HOLDINGS PLC (HOLDINGS)", None),
        ("EASTGATE", None),
        # One token is a payer who wrote only a number, not a name followed by a reference.
        ("0600", None),
    ],
)
def test_the_typed_reference_is_the_last_token_when_it_carries_a_digit(
    description: str, fragment: str | None
) -> None:
    assert reference_fragment(description) == fragment


def test_the_slices_references_are_found_and_only_those(rows: tuple[BankRow, ...]) -> None:
    found = Counter(
        row.txn_type == "TFR" for row in rows if reference_fragment(row.description) is not None
    )
    assert found == {False: PAYER_REFERENCES, True: PAYOUT_REFERENCES}
    # Every payer reference is one of the four forms spec 01 §3.2 lists -- no name token was
    # mistaken for one.
    forms: Counter[str] = Counter()
    for row in rows:
        fragment = reference_fragment(row.description)
        if fragment is None or row.txn_type == "TFR":
            continue
        for form, pattern in (
            ("exact", r"INV-\d{4}-\d{4}"),
            ("compact", r"INV\d{4}"),
            ("digits", r"\d{4}"),
            ("po", r"PO\d{5}"),
        ):
            if re.fullmatch(pattern, fragment):
                forms[form] += 1
    assert forms == {"exact": 130, "compact": 86, "digits": 94, "po": 75}
    assert sum(forms.values()) == PAYER_REFERENCES


def test_a_full_invoice_reference_names_the_invoice_ground_truth_says_was_paid(
    rows: tuple[BankRow, ...], manifest_groups: list[dict[str, Any]]
) -> None:
    group_of = {target: group for group in manifest_groups for target in group["target_ids"]}
    checked = 0
    for row in rows:
        fragment = reference_fragment(row.description)
        if fragment is None or not re.fullmatch(r"INV-\d{4}-\d{4}", fragment):
            continue
        group = group_of[row.line_id]
        if group.get("duplicate_of"):
            group = group_of[group["duplicate_of"]]
        assert f"erp:{fragment}" in group["source_ids"], row.line_id
        checked += 1
    assert checked == 130


# -- who a line is from ----------------------------------------------------------------------


def test_a_processor_payout_is_never_put_through_entity_resolution(
    rows: tuple[BankRow, ...],
) -> None:
    def must_not_be_called(description: str) -> PartyRef:
        raise AssertionError(f"resolved a payout line: {description!r}")

    payouts = [row for row in rows if row.txn_type == "TFR"]
    assert len(payouts) == BY_TYPE["TFR"]
    for row in payouts:
        party = party_for(row, must_not_be_called)
        assert party.method == "unresolved"
        assert party.counterparty_id is None
        assert party.surface == row.description


def test_every_other_line_is_resolved_through_the_shared_resolver(
    rows: tuple[BankRow, ...],
) -> None:
    seen: list[str] = []

    def spy(description: str) -> PartyRef:
        seen.append(description)
        return unresolved(description)

    for row in rows:
        party_for(row, spy)
    assert len(seen) == LINES - BY_TYPE["TFR"]


def test_the_offline_service_maps_every_line(offline: BankService) -> None:
    lines = offline.export_bank_lines(limit=200)
    assert len(lines) == 200
    assert all(line.source == "bank" and line.event_time is not None for line in lines)

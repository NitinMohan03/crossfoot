"""Each agent-facing tool answers a known query correctly (plan §4.4 verification).

Driven through `BankService` rather than through MCP: rule 1 puts every decision in the tool
layer, so this is where correctness is tested, and `test_integration.py` proves the wiring once.
The `bank` fixture is the server as it really runs -- files from the committed slice, names and
aliases from Postgres through the read-only role.
"""

from collections import Counter
from datetime import UTC, date, datetime

import pytest

from crossfoot.mcp.bank import BankService
from crossfoot.mcp.bank.parties import StaticParties
from crossfoot.models.canonical import BankDetail, CanonicalRecord
from tests.test_mcp.bank.conftest import BY_TYPE, LINES
from tests.test_mcp.conftest import COUNTERPARTIES


def walk(service: BankService, page_size: int = 200) -> list[CanonicalRecord]:
    """Every line, paged the way ingestion pages."""
    seen: list[CanonicalRecord] = []
    after: str | None = None
    while page := service.export_bank_lines(after_id=after, limit=page_size):
        seen.extend(page)
        after = page[-1].record_id
    return seen


# -- get_bank_line ---------------------------------------------------------------------------


def test_get_bank_line_returns_a_canonical_payment(bank: BankService) -> None:
    line = bank.get_bank_line("bank:2024-03-01:000001")
    assert (line.record_id, line.source, line.role) == ("bank:2024-03-01:000001", "bank", "payment")
    assert line.amount.minor == 249_230
    assert line.record_date == date(2024, 3, 1)
    assert line.event_time == datetime(2024, 3, 1, 1, 57, tzinfo=UTC)
    assert line.event_time_source == "2024-03-01T01:57:00+00:00"
    assert line.free_text == "MERIDIAN AEROSPACE INV0014"
    assert line.reference_surface == "INV0014"
    assert line.detail == BankDetail(txn_type="FPI", balance_minor=52_776_853)
    # The name is `MERIDIAN AEROSPACE LLP` less its suffix: identified structurally, at full
    # score, even with a reference typed after it.
    assert (line.party.counterparty_id, line.party.method, line.party.similarity) == (
        50,
        "normalised",
        100,
    )
    assert line.party.surface == "MERIDIAN AEROSPACE INV0014"


def test_get_bank_line_accepts_the_id_without_its_prefix(bank: BankService) -> None:
    assert bank.get_bank_line("2024-03-01:000001") == bank.get_bank_line("bank:2024-03-01:000001")


def test_get_bank_line_straddles_the_clock_change(bank: BankService) -> None:
    winter = bank.get_bank_line("bank:2024-03-28:000091")
    summer = bank.get_bank_line("bank:2024-04-02:000003")
    assert winter.event_time == datetime(2024, 3, 28, 23, 0, tzinfo=UTC)
    assert summer.event_time == datetime(2024, 4, 1, 23, 2, tzinfo=UTC)
    assert (winter.event_time_source, summer.event_time_source) == (
        "2024-03-28T23:00:00+00:00",
        "2024-04-02T00:02:00+01:00",
    )
    # UTC says the 1st; the value date is a calendar date and says the 2nd. Both are kept.
    assert summer.record_date == date(2024, 4, 2)


def test_a_payout_line_names_its_batch_and_no_counterparty(bank: BankService) -> None:
    line = bank.get_bank_line("bank:2024-01-12:000002")
    assert line.free_text == "PAYCO SETTLE BAT_2024-01-11_01"
    assert line.reference_surface == "BAT_2024-01-11_01"
    assert line.amount.minor == 285_266
    assert isinstance(line.detail, BankDetail)
    assert line.detail.txn_type == "TFR"
    assert (line.party.method, line.party.counterparty_id) == ("unresolved", None)


def test_a_refund_is_negative_and_names_who_it_went_to(bank: BankService) -> None:
    line = bank.get_bank_line("bank:2024-02-22:000047")
    assert line.amount.minor == -17_150
    assert line.amount.base_minor == -17_150
    assert line.party.method == "exact"
    assert line.party.counterparty_id is not None


# -- find_bank_lines -------------------------------------------------------------------------


def test_find_bank_lines_on_one_value_date(bank: BankService) -> None:
    lines, total = bank.find_bank_lines(date_from="2024-06-03", date_to="2024-06-03")
    assert (len(lines), total) == (8, 8)
    assert [line.record_id for line in lines] == [
        f"bank:2024-06-03:{row:06d}" for row in range(1, 9)
    ]
    assert {line.record_date for line in lines} == {date(2024, 6, 3)}


def test_find_bank_lines_in_an_amount_window(bank: BankService) -> None:
    lines, total = bank.find_bank_lines(
        amount_min_minor=100_000, amount_max_minor=120_000, limit=200
    )
    assert (len(lines), total) == (59, 59)
    assert all(100_000 <= line.amount.minor <= 120_000 for line in lines)
    assert (lines[0].record_id, lines[-1].record_id) == (
        "bank:2024-01-31:000034",
        "bank:2025-01-02:000007",
    )
    # Statement order, which is id order.
    assert [line.record_id for line in lines] == sorted(line.record_id for line in lines)


def test_find_bank_lines_amounts_are_signed_so_refunds_are_findable(bank: BankService) -> None:
    lines, total = bank.find_bank_lines(amount_max_minor=-1)
    assert (len(lines), total) == (BY_TYPE["FPO"], BY_TYPE["FPO"])
    assert {line.detail.txn_type for line in lines} == {"FPO"}  # type: ignore[union-attr]
    assert all(line.amount.minor < 0 for line in lines)


def test_find_bank_lines_combines_both_windows_across_a_month_end(bank: BankService) -> None:
    lines, total = bank.find_bank_lines(
        date_from="2024-03-25", date_to="2024-04-05", amount_min_minor=500_000
    )
    assert (len(lines), total) == (13, 13)
    assert {line.record_date.month for line in lines} == {3, 4}
    assert all(date(2024, 3, 25) <= line.record_date <= date(2024, 4, 5) for line in lines)
    assert all(line.amount.minor >= 500_000 for line in lines)


def test_find_bank_lines_says_how_many_matched_when_the_page_is_smaller(
    bank: BankService,
) -> None:
    lines, total = bank.find_bank_lines(limit=5)
    assert len(lines) == 5
    assert total == LINES
    assert lines[0].record_id == "bank:2024-01-09:000001"


def test_an_open_ended_window_is_allowed_on_either_side(bank: BankService) -> None:
    _, before = bank.find_bank_lines(date_to="2024-05-31", limit=1)
    _, after = bank.find_bank_lines(date_from="2024-06-01", limit=1)
    assert (before, after) == (423, LINES - 423)


# -- export_bank_lines -----------------------------------------------------------------------


def test_export_pages_by_id_and_covers_the_slice_exactly(
    bank: BankService, manifest_bank_ids: set[str]
) -> None:
    seen = [line.record_id for line in walk(bank)]
    assert len(seen) == LINES
    assert len(set(seen)) == LINES
    assert seen == sorted(seen)
    assert set(seen) == manifest_bank_ids


def test_export_resumes_after_an_id_including_across_a_month_end(bank: BankService) -> None:
    # 2024-01 has 35 rows; the page after its last line starts February at row 1.
    page = bank.export_bank_lines(after_id="bank:2024-01-31:000035", limit=2)
    assert [line.record_id[:12] for line in page] == ["bank:2024-02", "bank:2024-02"]
    assert [line.record_id[-6:] for line in page] == ["000001", "000002"]
    # And mid-month it is simply the next row.
    following = bank.export_bank_lines(after_id="bank:2024-03-01:000001", limit=1)
    assert following[0].record_id.endswith(":000002")


def test_export_past_the_last_line_is_empty(bank: BankService) -> None:
    assert bank.export_bank_lines(after_id="bank:2025-01-20:000027") == []


@pytest.mark.parametrize("limit", [1, 7, 200])
def test_limits_are_honoured(bank: BankService, limit: int) -> None:
    assert len(bank.export_bank_lines(limit=limit)) == limit
    assert len(bank.find_bank_lines(limit=limit)[0]) == limit


def test_page_size_does_not_change_what_is_exported(bank: BankService) -> None:
    assert [line.record_id for line in walk(bank, 37)] == [line.record_id for line in walk(bank)]


# -- entity resolution -----------------------------------------------------------------------


def test_the_name_index_is_the_erps_counterparties(bank: BankService) -> None:
    assert len(bank.parties.name_index()) == COUNTERPARTIES


def test_the_registry_the_server_holds_is_frozen(bank: BankService) -> None:
    assert bank.parties.registry().frozen is True


def test_every_line_is_resolved_as_the_measurement_says(bank: BankService) -> None:
    methods = Counter(
        (line.detail.txn_type, line.party.method)  # type: ignore[union-attr]
        for line in walk(bank)
    )
    assert methods == {
        ("FPI", "exact"): 410,
        ("FPI", "normalised"): 201,
        ("FPI", "fuzzy"): 22,
        ("FPI", "unresolved"): 31,
        ("BGC", "exact"): 161,
        ("BGC", "normalised"): 87,
        ("BGC", "fuzzy"): 9,
        ("BGC", "unresolved"): 11,
        ("FPO", "exact"): 9,
        ("FPO", "normalised"): 1,
        ("TFR", "unresolved"): BY_TYPE["TFR"],
    }


def test_names_from_postgres_resolve_exactly_as_names_from_the_dump(
    bank: BankService, offline: BankService
) -> None:
    # The database is the server's source of names; the dump is what was loaded into it. If the
    # two ever resolved a line differently, one of them would not be the ERP.
    assert isinstance(offline.parties, StaticParties)
    assert [line.party for line in walk(bank)] == [line.party for line in walk(offline)]

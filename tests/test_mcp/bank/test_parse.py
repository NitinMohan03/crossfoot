"""The export's text becomes rows, exactly, or is refused by name (spec 01 §3.2).

No database and no MCP here: this is the format. What the generator actually emits is plain
comma-separated CSV -- no fixed-width columns, no quoting, pure ASCII, CRLF -- so the "encoding
quirks" the plan mentions are tested where they are real: a file that is not UTF-8 is a coded
error and not mojibake, and the two harmless variations (a byte-order mark, LF line endings) are
read as what they are.
"""

from collections import Counter
from pathlib import Path

import pytest

from crossfoot.generate.read import read_bank
from crossfoot.mcp.bank.files import ReadOnlyExport
from crossfoot.mcp.bank.parse import HEADER, BankRow, PostedAt, parse_month, parse_sidecar
from crossfoot.mcp.bank.settings import SIDECAR_NAME
from crossfoot.mcp.common.errors import MALFORMED_SOURCE, UNAVAILABLE, ToolError
from tests.test_mcp.bank.conftest import (
    BANK_DIR,
    BY_TYPE,
    CLOSING_BALANCE_MINOR,
    LINES,
    MONTHS,
    OPENING_BALANCE_MINOR,
    SUMMER,
    WINTER,
)

# -- the committed slice ---------------------------------------------------------------------


def test_one_file_per_month_january_to_january(export: ReadOnlyExport) -> None:
    months = export.months()
    assert len(months) == MONTHS
    assert (months[0], months[-1]) == ("2024-01", "2025-01")
    # The sidecar shares the directory and is not mistaken for a statement.
    assert all(len(month) == 7 for month in months)


def test_every_row_parses_and_the_counts_are_the_slices(rows: tuple[BankRow, ...]) -> None:
    assert len(rows) == LINES
    assert Counter(row.txn_type for row in rows) == BY_TYPE


def test_the_ids_are_exactly_the_ones_the_manifest_writes(
    rows: tuple[BankRow, ...], manifest_bank_ids: set[str]
) -> None:
    ids = [row.line_id for row in rows]
    assert len(set(ids)) == LINES
    # Set equality, both ways: no line the manifest lacks, no manifest id the export lacks.
    assert set(ids) == manifest_bank_ids


def test_this_parser_agrees_with_the_generators_own_reader(rows: tuple[BankRow, ...]) -> None:
    # Two implementations written independently, compared on everything that matters.
    oracle = read_bank(BANK_DIR)
    assert [
        (
            row.line_id,
            row.value_date,
            row.txn_type,
            row.description,
            row.paid_in_minor,
            row.paid_out_minor,
            row.balance_minor,
        )
        for row in rows
    ] == [
        (
            line.id,
            line.value_date,
            line.txn_type,
            line.description,
            line.paid_in_minor,
            line.paid_out_minor,
            line.balance_minor,
        )
        for line in oracle
    ]


def test_amounts_are_integer_minor_units_and_the_account_adds_up(
    rows: tuple[BankRow, ...],
) -> None:
    assert all(type(row.amount_minor) is int for row in rows)
    assert all((row.paid_in_minor > 0) != (row.paid_out_minor > 0) for row in rows)
    # Only the ten refunds go out.
    assert sum(1 for row in rows if row.amount_minor < 0) == BY_TYPE["FPO"]
    assert {row.txn_type for row in rows if row.amount_minor < 0} == {"FPO"}
    # The running balance carries across month files too, which no single file's check sees.
    assert rows[0].balance_minor - rows[0].amount_minor == OPENING_BALANCE_MINOR
    assert OPENING_BALANCE_MINOR + sum(row.amount_minor for row in rows) == CLOSING_BALANCE_MINOR
    assert rows[-1].balance_minor == CLOSING_BALANCE_MINOR


def test_a_row_keeps_its_own_text(rows: tuple[BankRow, ...]) -> None:
    first_of_march = next(row for row in rows if row.month == "2024-03")
    assert first_of_march.line_id == "bank:2024-03-01:000001"
    assert first_of_march.raw == "01/03/2024,FPI,MERIDIAN AEROSPACE INV0014,,2492.30,527768.53"
    assert (first_of_march.paid_in_minor, first_of_march.balance_minor) == (249_230, 52_776_853)


def test_no_description_is_over_the_banks_32_characters(rows: tuple[BankRow, ...]) -> None:
    assert max(len(row.description) for row in rows) == 32


def test_the_sidecar_covers_every_line_and_both_sides_of_the_clock_change(
    rows: tuple[BankRow, ...], posted: dict[str, PostedAt]
) -> None:
    assert set(posted) == {row.line_id for row in rows}
    offsets = Counter(entry.source[-6:] for entry in posted.values())
    assert offsets == {"+01:00": SUMMER, "+00:00": WINTER}
    # Whatever the offset was, what came out is UTC.
    assert {entry.moment.utcoffset().total_seconds() for entry in posted.values()} == {0}  # type: ignore[union-attr]


# -- the two tolerances ----------------------------------------------------------------------

GOOD = (
    f"{HEADER}\r\n"
    "01/03/2024,FPI,ACME WIDGETS LTD 0891,,100.00,1100.00\r\n"
    "01/03/2024,FPO,ACME WIDGETS LTD,25.50,,1074.50\r\n"
    "04/03/2024,BGC,NORTHLIGHT,,0.01,1074.51\r\n"
)


def test_the_small_statement_parses() -> None:
    parsed = parse_month("2024-03", GOOD)
    assert [row.line_id for row in parsed] == [
        "bank:2024-03-01:000001",
        "bank:2024-03-01:000002",
        "bank:2024-03-04:000003",
    ]
    assert [row.amount_minor for row in parsed] == [10_000, -2_550, 1]


def test_lf_line_endings_are_the_same_rows() -> None:
    assert parse_month("2024-03", GOOD.replace("\r\n", "\n")) == parse_month("2024-03", GOOD)


def test_a_byte_order_mark_is_tolerated(tmp_path: Path) -> None:
    (tmp_path / "2024-03.csv").write_bytes(b"\xef\xbb\xbf" + GOOD.encode("utf-8"))
    text = ReadOnlyExport(tmp_path, SIDECAR_NAME).month_text("2024-03")
    assert text is not None
    assert parse_month("2024-03", text) == parse_month("2024-03", GOOD)


# -- everything else is refused, by file and row ---------------------------------------------


def row(number: int, replacement: str) -> str:
    """`GOOD` with data row `number` replaced."""
    lines = GOOD.split("\r\n")
    lines[number] = replacement
    return "\r\n".join(lines)


@pytest.mark.parametrize(
    ("text", "at_row", "says"),
    [
        ("", 0, "header"),
        (GOOD.replace("Paid Out,Paid In", "Paid In,Paid Out"), 0, "header"),
        (GOOD.replace(f"{HEADER}\r\n", ""), 0, "header"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,,100.00"), 1, "expected 6 fields"),
        (row(1, "01/03/2024,FPI,ACME, WIDGETS LTD,,100.00,1100.00"), 1, "expected 6 fields"),
        (row(1, "01/03/2024,FPI,ACME,,1,100.00,1100.00"), 1, "expected 6 fields"),
        (row(2, ""), 2, "expected 6 fields"),
        (row(1, "2024-03-01,FPI,ACME WIDGETS LTD,,100.00,1100.00"), 1, "DD/MM/YYYY"),
        (row(1, "1/3/2024,FPI,ACME WIDGETS LTD,,100.00,1100.00"), 1, "DD/MM/YYYY"),
        (row(1, "31/02/2024,FPI,ACME WIDGETS LTD,,100.00,1100.00"), 1, "not a real date"),
        (row(1, "01/04/2024,FPI,ACME WIDGETS LTD,,100.00,1100.00"), 1, "does not belong"),
        (row(1, "01/03/2024,CHQ,ACME WIDGETS LTD,,100.00,1100.00"), 1, "transaction type"),
        (row(1, "01/03/2024,fpi,ACME WIDGETS LTD,,100.00,1100.00"), 1, "transaction type"),
        (row(1, f"01/03/2024,FPI,{'A' * 33},,100.00,1100.00"), 1, "over the bank's 32"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,5.00,100.00,1100.00"), 1, "exactly one"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,,,1100.00"), 1, "exactly one"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,,100.0,1100.00"), 1, "two decimal places"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,,100,1100.00"), 1, "two decimal places"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,,1e2,1100.00"), 1, "two decimal places"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,,-100.00,1100.00"), 1, "two decimal places"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,, 100.00,1100.00"), 1, "two decimal places"),
        (row(1, "01/03/2024,FPI,ACME WIDGETS LTD,,100.00,£1100.00"), 1, "not a balance"),
        (row(2, "01/03/2024,FPO,ACME WIDGETS LTD,25.50,,1074.51"), 2, "running balance"),
        (row(3, "29/02/2024,BGC,NORTHLIGHT,,0.01,1074.51"), 3, "does not belong"),
    ],
)
def test_a_malformed_statement_is_refused_naming_the_file_and_the_row(
    text: str, at_row: int, says: str
) -> None:
    with pytest.raises(ToolError) as caught:
        parse_month("2024-03", text)
    assert caught.value.code == MALFORMED_SOURCE
    assert says in caught.value.message
    assert caught.value.extra == {"file": "2024-03.csv", "row": at_row}


def test_a_deleted_row_breaks_the_balance_chain_where_it_happened() -> None:
    # Ids are positions. Losing row 2 would silently renumber row 3; the balance says so.
    lines = GOOD.split("\r\n")
    del lines[2]
    with pytest.raises(ToolError) as caught:
        parse_month("2024-03", "\r\n".join(lines))
    assert caught.value.code == MALFORMED_SOURCE
    assert caught.value.extra["row"] == 2
    assert "missing, repeated or out of order" in caught.value.message


def test_two_rows_swapped_break_it_too() -> None:
    lines = GOOD.split("\r\n")
    lines[1], lines[2] = lines[2], lines[1]
    with pytest.raises(ToolError) as caught:
        parse_month("2024-03", "\r\n".join(lines))
    assert caught.value.code == MALFORMED_SOURCE


def test_value_dates_that_run_backwards_are_refused() -> None:
    text = (
        f"{HEADER}\r\n"
        "04/03/2024,FPI,ACME WIDGETS LTD,,100.00,1100.00\r\n"
        "01/03/2024,FPI,ACME WIDGETS LTD,,100.00,1200.00\r\n"
    )
    with pytest.raises(ToolError) as caught:
        parse_month("2024-03", text)
    assert (caught.value.code, caught.value.extra["row"]) == (MALFORMED_SOURCE, 2)


def test_bytes_that_are_not_utf8_are_a_coded_error_not_mojibake(tmp_path: Path) -> None:
    # A cp1252 export: the pound sign is the single byte 0xA3, which is not valid UTF-8.
    (tmp_path / "2024-03.csv").write_bytes(
        GOOD.replace("NORTHLIGHT", "NORTH £IGHT").encode("cp1252")
    )
    with pytest.raises(ToolError) as caught:
        ReadOnlyExport(tmp_path, SIDECAR_NAME).month_text("2024-03")
    assert caught.value.code == MALFORMED_SOURCE
    assert "not UTF-8" in caught.value.message
    assert "0xa3" in caught.value.message
    assert caught.value.extra == {"file": "2024-03.csv"}


def test_a_directory_that_is_not_there_is_unavailable(tmp_path: Path) -> None:
    absent = ReadOnlyExport(tmp_path / "nowhere", SIDECAR_NAME)
    for read in (absent.months, lambda: absent.month_text("2024-03"), absent.sidecar_text):
        with pytest.raises(ToolError) as caught:
            read()
        assert caught.value.code == UNAVAILABLE


def test_a_directory_with_no_statements_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a statement", encoding="utf-8")
    (tmp_path / "2024-3.csv").write_text("not a month file either", encoding="utf-8")
    with pytest.raises(ToolError) as caught:
        ReadOnlyExport(tmp_path, SIDECAR_NAME).months()
    assert caught.value.code == UNAVAILABLE


def test_a_month_with_no_file_is_simply_absent(export: ReadOnlyExport) -> None:
    assert export.month_text("2023-12") is None


# -- the sidecar -----------------------------------------------------------------------------


def test_a_sidecar_line_becomes_utc_and_keeps_its_own_text() -> None:
    posted = parse_sidecar(
        SIDECAR_NAME, '{"id":"bank:2024-07-01:000001","posted_at":"2024-07-01T09:30:00+01:00"}\n'
    )
    entry = posted["bank:2024-07-01:000001"]
    assert entry.moment.isoformat() == "2024-07-01T08:30:00+00:00"
    assert entry.source == "2024-07-01T09:30:00+01:00"


@pytest.mark.parametrize(
    ("line", "says"),
    [
        ('{"id":"bank:2024-07-01:000001","posted_at":"2024-07-01T09:30:00"}', "no UTC offset"),
        ('{"id":"bank:2024-07-01:000001","posted_at":"yesterday"}', "ISO-8601"),
        ('{"id":"bank:2024-07-01:000001","posted_at":1719826200}', "string"),
        ('{"id":"bank:2024-07-01:000001"}', "string"),
        ('["bank:2024-07-01:000001","2024-07-01T09:30:00+01:00"]', "JSON object"),
        ("bank:2024-07-01:000001,2024-07-01T09:30:00+01:00", "JSON object"),
    ],
)
def test_a_malformed_sidecar_line_is_refused(line: str, says: str) -> None:
    with pytest.raises(ToolError) as caught:
        parse_sidecar(SIDECAR_NAME, line + "\n")
    assert caught.value.code == MALFORMED_SOURCE
    assert says in caught.value.message
    assert caught.value.extra == {"file": SIDECAR_NAME, "row": 1}


def test_an_id_posted_twice_is_refused() -> None:
    line = '{"id":"bank:2024-07-01:000001","posted_at":"2024-07-01T09:30:00+01:00"}\n'
    with pytest.raises(ToolError) as caught:
        parse_sidecar(SIDECAR_NAME, line + line)
    assert (caught.value.code, caught.value.extra["row"]) == (MALFORMED_SOURCE, 2)

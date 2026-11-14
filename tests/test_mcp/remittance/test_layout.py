"""A PDF becomes text runs with positions, or is refused by name. The library boundary.

No database, no MCP, and no advice yet: this is only "what is drawn where". The unit is the
run -- one drawn string -- because that is what lets the parser read the page as a form. Every
advice here is from a developed layout (see `conftest.py`).
"""

from pathlib import Path

import pytest

from crossfoot.generate.records import RemitLine
from crossfoot.mcp.common.errors import MALFORMED_SOURCE, UNAVAILABLE, ToolError
from crossfoot.mcp.remittance.files import PAYMENT_REF, ReadOnlyAdvices, ref_key
from crossfoot.mcp.remittance.layout import Page, read_pages
from crossfoot.mcp.remittance.settings import FX_FILE, MAX_BYTES, MAX_PAGES, REMIT_SUBDIR
from tests.test_mcp.remittance.conftest import (
    DEVELOPED,
    REMIT_DIR,
    SAMPLE_NAME,
    pages_of,
    rendered,
)

# -- the files -------------------------------------------------------------------------------


def test_the_listing_is_the_developed_advices_in_order(advices: ReadOnlyAdvices) -> None:
    refs = advices.payment_refs()
    assert len(refs) == DEVELOPED
    assert all(PAYMENT_REF.match(ref) for ref in refs)
    assert list(refs) == sorted(refs, key=ref_key)
    assert refs[0] == "PAY-2024-0001"


def test_the_truth_file_beside_the_advices_is_not_an_advice() -> None:
    # The committed directory holds `remit.jsonl` too. It is not listed, so it is never opened.
    listed = ReadOnlyAdvices(REMIT_DIR, REMIT_DIR.parent / FX_FILE, MAX_BYTES).payment_refs()
    assert len(listed) == len(list(REMIT_DIR.glob("*.pdf")))
    assert (REMIT_DIR / "remit.jsonl").is_file()
    assert not any("remit" in ref for ref in listed)


def test_the_order_is_numeric_once_the_numbers_outgrow_four_digits(tmp_path: Path) -> None:
    for name in ("PAY-2024-10000.pdf", "PAY-2024-9999.pdf", "PAY-2025-0001.pdf", "notes.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-")
    (tmp_path / "PAY-2024-0001.txt").write_bytes(b"not an advice")
    refs = ReadOnlyAdvices(tmp_path, tmp_path / FX_FILE, MAX_BYTES).payment_refs()
    assert refs == ("PAY-2024-9999", "PAY-2024-10000", "PAY-2025-0001")


def test_an_advice_that_was_never_sent_is_none_and_a_missing_dataset_is_unavailable(
    advices: ReadOnlyAdvices, tmp_path: Path
) -> None:
    assert advices.pdf_bytes("PAY-2024-9999") is None
    gone = ReadOnlyAdvices(tmp_path / "nowhere" / REMIT_SUBDIR, tmp_path / FX_FILE, MAX_BYTES)
    for call in (gone.payment_refs, lambda: gone.pdf_bytes("PAY-2024-0001")):
        with pytest.raises(ToolError) as caught:
            call()
        assert caught.value.code == UNAVAILABLE


@pytest.mark.parametrize(
    "not_a_reference",
    ["../erp", "..\\erp", "PAY-2024-0001/../../erp", "PAY-2024-0001.pdf", "pay-2024-0001", ""],
)
def test_only_a_payment_reference_ever_becomes_a_file_name(
    advices: ReadOnlyAdvices, not_a_reference: str
) -> None:
    # A programming error, not an answer: the tool layer has already refused such an argument.
    with pytest.raises(ValueError, match="not a payment reference"):
        advices.pdf_bytes(not_a_reference)


def test_a_file_over_the_size_cap_is_refused_without_being_read_to_the_end(
    tmp_path: Path,
) -> None:
    (tmp_path / SAMPLE_NAME).write_bytes(b"%PDF-" + b"0" * 5000)
    small = ReadOnlyAdvices(tmp_path, tmp_path / FX_FILE, max_bytes=1000)
    with pytest.raises(ToolError) as caught:
        small.pdf_bytes("PAY-2024-0088")
    assert caught.value.code == MALFORMED_SOURCE
    assert caught.value.extra == {"file": SAMPLE_NAME}


def test_the_rate_table_is_text_or_absent_or_refused(tmp_path: Path) -> None:
    source = ReadOnlyAdvices(tmp_path, tmp_path / FX_FILE, MAX_BYTES)
    assert source.fx_text() is None
    (tmp_path / FX_FILE).write_bytes(b"\xef\xbb\xbfdate,USD,EUR\n")
    assert source.fx_text() == "date,USD,EUR\n"
    (tmp_path / FX_FILE).write_bytes(b"date,USD,EUR\n2024-01-01,1.27\xff,1.16\n")
    with pytest.raises(ToolError) as caught:
        source.fx_text()
    assert caught.value.code == MALFORMED_SOURCE
    assert caught.value.extra == {"file": FX_FILE}


# -- runs and rows ---------------------------------------------------------------------------


def test_a_run_is_one_drawn_string_and_a_row_is_one_baseline() -> None:
    (page,) = pages_of(rendered("A"))
    assert [row.texts for row in page.rows] == [
        ("REMITTANCE ADVICE",),
        ("From:", "ACME WIDGETS LTD"),
        ("Payment reference:", "PAY-2024-0088"),
        ("Payment date:", "2024-03-14"),
        ("Total:", "£5650.50"),
        ("Invoice", "Amount", "Note"),
        ("INV-2024-0891", "4417.00"),
        ("INV-2024-0902", "1283.50", "rounding adjustment"),
        ("CN-2024-0007", "-50.00", "credit note"),
        ("TOTAL", "5650.50"),
        ("Payment reduced pending resolution. We hold CN-2025-0031 against INV-2024-0891.",),
    ]
    assert [row.number for row in page.rows] == list(range(1, 12))


def test_spaces_inside_a_run_are_kept_exactly() -> None:
    (page,) = pages_of(rendered("C", payer="HALDANE FOODS AND CO"))
    # Three spaces between the name and the reference, as drawn: the parser splits on them.
    assert page.rows[1].texts == ("HALDANE FOODS AND CO   PAY-2024-0088",)


def test_positions_are_integers_and_columns_line_up() -> None:
    (page,) = pages_of(rendered("A"))
    runs = [run for row in page.rows for run in row.runs]
    assert all(type(value) is int for run in runs for value in (run.x0, run.x1, run.top))
    assert all(run.x0 < run.x1 for run in runs)
    heading = {run.text: run for run in page.rows[5].runs}
    for row in page.rows[6:9]:
        assert row.runs[0].x0 == heading["Invoice"].x0
        assert row.runs[1].x0 == heading["Amount"].x0
    # Rows are top to bottom, and a row's runs left to right.
    tops = [row.runs[0].top for row in page.rows]
    assert tops == sorted(tops)
    assert all(
        [run.x0 for run in row.runs] == sorted(run.x0 for run in row.runs) for row in page.rows
    )


def test_right_aligned_amounts_share_a_right_edge_not_a_left_one() -> None:
    (page,) = pages_of(rendered("C"))
    heading = page.rows[3].runs[0]
    assert heading.text == "Amount"
    amounts = [row.runs[0] for row in page.rows[4:8]]
    assert [run.text for run in amounts] == ["£4417.00", "£1283.50", "-£50.00", "£5650.50"]
    assert {run.x1 for run in amounts} == {heading.x1}
    assert len({run.x0 for run in amounts}) > 1


@pytest.mark.parametrize(("currency", "symbol"), [("GBP", "£"), ("USD", "$"), ("EUR", "€")])
def test_every_currency_symbol_decodes_to_its_character(currency: str, symbol: str) -> None:
    for layout in ("A", "B", "C"):
        (page,) = pages_of(rendered(layout, currency=currency))
        text = " ".join(text for row in page.rows for text in row.texts)
        assert symbol in text, layout
        assert "(cid:" not in text


def test_a_long_table_is_several_pages() -> None:
    lines = [RemitLine(f"INV-2024-{number:04d}", 10_000 + number) for number in range(30)]
    first, second = pages_of(rendered("C", lines=lines))
    assert (first.number, second.number) == (1, 2)
    assert first.rows[2].texts == ("Paid 2024-03-14", "Page 1 of 2")
    assert first.rows[-1].texts == ("continued overleaf",)
    assert second.rows[2].texts == ("Paid 2024-03-14", "Page 2 of 2")
    # Row numbers restart on each page, which is what an error message counts in.
    assert second.rows[0].number == 1


def test_every_developed_advice_reads_and_holds_nothing_undecodable(
    developed_pages: dict[str, tuple[Page, ...]],
) -> None:
    assert len(developed_pages) == DEVELOPED
    assert {len(pages) for pages in developed_pages.values()} == {1}
    odd = {
        char
        for pages in developed_pages.values()
        for page in pages
        for row in page.rows
        for text in row.texts
        for char in text
        if not char.isascii()
    }
    assert odd == {"£", "€"}


# -- what is not a PDF -----------------------------------------------------------------------


def damaged() -> dict[str, bytes]:
    good = rendered("A")
    return {
        "empty": b"",
        "a letter": b"Dear Sir, please find our remittance attached.\n",
        "a web page": b"<html><body>404 Not Found</body></html>",
        "a header and noise": b"%PDF-1.4\n" + b"\x00\xff noise " * 50,
        "a header and nothing": b"%PDF-",
        "the first half": good[: len(good) // 2],
        "all but the end": good[:-200],
    }


@pytest.mark.parametrize("kind", sorted(damaged()))
def test_bytes_that_are_not_a_readable_pdf_are_refused_by_name(kind: str) -> None:
    with pytest.raises(ToolError) as caught:
        read_pages(SAMPLE_NAME, damaged()[kind], max_pages=MAX_PAGES)
    assert caught.value.code == MALFORMED_SOURCE
    assert caught.value.extra == {"file": SAMPLE_NAME}
    assert caught.value.message.startswith(f"{SAMPLE_NAME}: ")


def test_what_the_library_raised_is_named_and_its_text_is_not_repeated() -> None:
    with pytest.raises(ToolError) as caught:
        read_pages(SAMPLE_NAME, damaged()["a header and noise"], max_pages=MAX_PAGES)
    # The kind of failure, for a person; not a stack of parser internals for an agent to read.
    assert caught.value.message == f"{SAMPLE_NAME}: the PDF could not be read (PdfminerException)"
    assert caught.value.__cause__ is not None


def test_more_pages_than_an_advice_has_is_refused() -> None:
    lines = [RemitLine(f"INV-2024-{number:04d}", 10_000 + number) for number in range(60)]
    raw = rendered("C", lines=lines)
    assert len(read_pages(SAMPLE_NAME, raw, max_pages=3)) == 3
    with pytest.raises(ToolError) as caught:
        read_pages(SAMPLE_NAME, raw, max_pages=2)
    assert caught.value.code == MALFORMED_SOURCE
    assert "3 pages" in caught.value.message

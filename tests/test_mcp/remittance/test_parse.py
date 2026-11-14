"""Text runs become an advice, whole and exact, or are refused by name (spec 01 §3.4, §6).

No database and no MCP here: this is the form. Two kinds of evidence:

* **The committed slice.** Every advice in a developed layout is parsed and compared with
  `remit.jsonl` field by field -- payer, reference, date, total, every line's reference, amount
  and note, and the paragraph. `remit.jsonl` is what the generator *meant*; the parser never saw
  it, so agreement is a result and not a tautology.
* **Advices that should not read.** Drawn by the generator with a contradiction in them, or
  edited run by run, so each refusal is tested on its own.

The held-out layout does not appear in this file (see `conftest.py`).
"""

from collections import Counter
from datetime import date
from typing import Any

import pytest

from crossfoot.generate.records import RemitLine
from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError
from crossfoot.mcp.remittance.layout import Page
from crossfoot.mcp.remittance.parse import (
    NO_HEADER,
    Advice,
    Amount,
    Line,
    parse_advice,
    parse_amount,
)
from tests.test_mcp.remittance.conftest import (
    BY_LAYOUT,
    DEVELOPED,
    DEVELOPED_LAYOUTS,
    FOREIGN_DEVELOPED,
    SAMPLE_NAME,
    SAMPLE_NARRATIVE,
    pages_of,
    read,
    rendered,
    swapped,
    without_row,
)

SYMBOL_OF = {"GBP": "£", "USD": "$", "EUR": "€"}


def as_truth(advice: Advice) -> dict[str, Any]:
    """An advice in the shape `remit.jsonl` writes one, minus the two things a PDF cannot say."""
    return {
        "payer": advice.payer,
        "payment_ref": advice.payment_ref,
        "paid_on": advice.paid_on.isoformat(),
        "total_minor": advice.total_minor,
        "lines": [
            {"invoice_ref": line.invoice_ref, "amount_minor": line.amount_minor, "note": line.note}
            for line in advice.lines
        ],
        "narrative": advice.narrative,
    }


FIELDS = ("payer", "payment_ref", "paid_on", "total_minor", "lines", "narrative")


def refusal(pages: tuple[Page, ...]) -> ToolError:
    with pytest.raises(ToolError) as caught:
        parse_advice(SAMPLE_NAME, pages)
    assert caught.value.code == MALFORMED_SOURCE
    assert caught.value.extra["file"] == SAMPLE_NAME
    assert caught.value.message.startswith(f"{SAMPLE_NAME} page ")
    return caught.value


# -- the committed slice ---------------------------------------------------------------------


def test_every_developed_advice_parses(
    parsed: dict[str, Advice], developed: list[dict[str, Any]]
) -> None:
    assert len(parsed) == len(developed) == DEVELOPED
    by_layout = Counter(row["template"] for row in developed)
    assert by_layout == {layout: BY_LAYOUT[layout] for layout in DEVELOPED_LAYOUTS}


def test_every_field_of_every_developed_advice_is_what_the_generator_wrote(
    parsed: dict[str, Advice], developed: list[dict[str, Any]]
) -> None:
    exact: Counter[str] = Counter()
    for row in developed:
        got = as_truth(parsed[row["payment_ref"]])
        for field in FIELDS:
            assert got[field] == row[field], (row["payment_ref"], field)
        exact[row["template"]] += 1
    # 444 of 444, by layout: the parse rate on the layouts it was developed against.
    assert exact == {"A": 132, "B": 146, "C": 166}


def test_what_the_slice_exercises(parsed: dict[str, Advice]) -> None:
    advices = list(parsed.values())
    lines = [line for advice in advices for line in advice.lines]
    assert len(lines) == 899
    assert Counter(len(advice.lines) for advice in advices) == {1: 233, 2: 48, 3: 82, 4: 81}
    # Credit notes are negative lines, as the canonical sign convention has them.
    negative = [line for line in lines if line.amount_minor < 0]
    assert len(negative) == 36
    assert {line.invoice_ref[:3] for line in negative} == {"CN-"}
    assert {line.note for line in negative} == {"credit note"}
    assert sum(1 for line in lines if line.note is not None) == 408
    assert sum(1 for advice in advices if advice.narrative is not None) == 220
    # Thirty paragraphs are long enough to wrap, and come back as the one string they were.
    assert sum(1 for advice in advices if advice.narrative and len(advice.narrative) > 92) == 30


def test_the_currency_symbol_is_the_currency_the_invoices_are_in(
    parsed: dict[str, Advice], developed: list[dict[str, Any]], currency_of: dict[str, str]
) -> None:
    foreign: Counter[str] = Counter()
    for row in developed:
        currency = currency_of[row["id"]]
        assert parsed[row["payment_ref"]].symbol == SYMBOL_OF[currency], row["payment_ref"]
        if currency != "GBP":
            foreign[currency] += 1
    assert foreign == FOREIGN_DEVELOPED


def test_the_lines_of_every_advice_add_up_to_its_total(parsed: dict[str, Advice]) -> None:
    for ref, advice in parsed.items():
        assert sum(line.amount_minor for line in advice.lines) == advice.total_minor, ref
        assert advice.total_minor > 0


# -- each form, drawn on purpose -------------------------------------------------------------


@pytest.mark.parametrize("layout", DEVELOPED_LAYOUTS)
def test_the_same_advice_reads_the_same_in_every_developed_layout(layout: str) -> None:
    assert read(rendered(layout)) == Advice(
        payer="ACME WIDGETS LTD",
        payment_ref="PAY-2024-0088",
        paid_on=date(2024, 3, 14),
        symbol="£",
        total_minor=565_050,
        lines=(
            Line("INV-2024-0891", 441_700, None),
            Line("INV-2024-0902", 128_350, "rounding adjustment"),
            Line("CN-2024-0007", -5_000, "credit note"),
        ),
        narrative=SAMPLE_NARRATIVE,
        pages=1,
    )


@pytest.mark.parametrize("layout", DEVELOPED_LAYOUTS)
@pytest.mark.parametrize(("currency", "symbol"), [("USD", "$"), ("EUR", "€")])
def test_a_foreign_advice_carries_its_symbol(layout: str, currency: str, symbol: str) -> None:
    advice = read(rendered(layout, currency=currency))
    assert (advice.symbol, advice.total_minor) == (symbol, 565_050)


@pytest.mark.parametrize("layout", DEVELOPED_LAYOUTS)
def test_an_advice_with_no_paragraph_has_none(layout: str) -> None:
    assert read(rendered(layout, narrative=None)).narrative is None


@pytest.mark.parametrize("layout", DEVELOPED_LAYOUTS)
def test_a_wrapped_paragraph_comes_back_as_one_string(layout: str) -> None:
    long = (
        "Our system rounds each line before transfer, so line totals differ from your invoices "
        "by pence. Please allocate line by line and write off the difference at your end."
    )
    raw = rendered(layout, narrative=long)
    assert sum(1 for row in pages_of(raw)[0].rows if len(row.runs) == 1) >= 3
    assert read(raw).narrative == long


@pytest.mark.parametrize("layout", DEVELOPED_LAYOUTS)
def test_a_payer_name_is_kept_character_for_character(layout: str) -> None:
    name = "MARCHMONT AEROSPACE LIMITED (UK)"
    assert read(rendered(layout, payer=name)).payer == name
    assert (
        read(rendered(layout, payer="STANWICK UTILITIES & CO")).payer == "STANWICK UTILITIES & CO"
    )


@pytest.mark.parametrize("count", [25, 26, 30, 60])
def test_a_table_that_runs_over_pages_is_one_advice(count: int) -> None:
    lines = [RemitLine(f"INV-2024-{number:04d}", 10_000 + number) for number in range(count)]
    advice = read(rendered("C", lines=lines))
    assert advice.pages == (count + 24) // 25
    assert [line.invoice_ref for line in advice.lines] == [line.invoice_ref for line in lines]
    assert advice.total_minor == sum(line.amount_minor for line in lines)
    assert advice.narrative == SAMPLE_NARRATIVE


# -- amounts ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4417.00", Amount(441_700, "")),
        ("£4417.00", Amount(441_700, "£")),
        ("-£50.00", Amount(-5_000, "£")),
        ("-50.00", Amount(-5_000, "")),
        ("$0.05", Amount(5, "$")),
        ("€1234567.89", Amount(123_456_789, "€")),
        ("0.00", Amount(0, "")),
    ],
)
def test_an_amount_is_integer_minor_units_built_from_its_digits(
    text: str, expected: Amount
) -> None:
    parsed = parse_amount(text)
    assert parsed == expected
    assert parsed is not None and type(parsed.minor) is int


@pytest.mark.parametrize(
    "text",
    [
        "1,250.00",  # a separator: a different sender, not a rounding choice
        "12.5",
        "12",
        "12.500",
        "£-50.00",
        "50.00-",
        "(50.00)",
        "GBP 50.00",
        "£ 50.00",
        "1.2e3",
        "",
        "TOTAL",
    ],
)
def test_anything_else_is_not_an_amount(text: str) -> None:
    assert parse_amount(text) is None


# -- advices that contradict themselves ------------------------------------------------------


@pytest.mark.parametrize("layout", DEVELOPED_LAYOUTS)
def test_lines_that_do_not_add_up_to_the_stated_total_are_refused(layout: str) -> None:
    # Drawn with a total one penny more than its lines. The stated total is not "corrected".
    error = refusal(pages_of(rendered(layout, total_minor=565_051)))
    assert "the lines add up to 565050" in error.message
    assert "states a total of 565051" in error.message
    assert error.extra == {"file": SAMPLE_NAME, "page": 1, "row": 0}


def test_two_statements_of_the_total_must_agree() -> None:
    pages = swapped(pages_of(rendered("A")), "£5650.50", "£5650.05")
    error = refusal(pages)
    assert "two different totals" in error.message
    assert "565050" in error.message and "565005" in error.message


def test_a_running_balance_must_follow_from_the_line_above() -> None:
    pages = swapped(pages_of(rendered("B")), "1233.50", "1233.05")
    error = refusal(pages)
    assert "running balance states 123305 where 123350" in error.message
    assert error.extra == {"file": SAMPLE_NAME, "page": 1, "row": 8}


def test_amounts_in_two_currencies_are_refused() -> None:
    error = refusal(swapped(pages_of(rendered("C")), "£1283.50", "€1283.50"))
    assert "more than one currency" in error.message


def test_an_advice_with_no_currency_symbol_anywhere_is_refused() -> None:
    # The one symbol this form prints is on the header total.
    error = refusal(swapped(pages_of(rendered("A")), "£5650.50", "5650.50"))
    assert "no currency symbol" in error.message


# -- runs that are not what the form needs ---------------------------------------------------


@pytest.mark.parametrize(
    ("layout", "old", "new", "says", "row"),
    [
        ("A", "4417.00", "4,417.00", "'4,417.00' is not an amount", 7),
        ("B", "0.00", "nil", "the balance 'nil' is not an amount", 10),
        ("C", "£5650.50", "5650.5", "the total '5650.5' is not an amount", 8),
        ("B", "PAID £5650.50", "PAID GBP5650.50", "the amount paid 'GBP5650.50'", 11),
        ("A", "INV-2024-0891", "PO88766", "'PO88766' is not an invoice or credit-note", 7),
        ("C", "CN-2024-0007", "see below", "'see below' is not an invoice", 7),
        ("A", "2024-03-14", "14/03/2024", "'14/03/2024' is not a YYYY-MM-DD date", 4),
        ("B", "2024-03-14", "2024-02-30", "'2024-02-30' is not a real date", 5),
        ("C", "Paid 2024-03-14", "Paid 14 March", f"{NO_HEADER}: 'Paid 14 March'", 3),
        ("A", "PAY-2024-0088", "0088", "'0088' is not a payment reference", 3),
        ("B", "Ref PAY-2024-0088", "PAY-2024-0088", "is not `Ref <reference>`", 4),
    ],
)
def test_a_value_of_the_wrong_shape_is_refused_where_it_stands(
    layout: str, old: str, new: str, says: str, row: int
) -> None:
    error = refusal(swapped(pages_of(rendered(layout)), old, new))
    assert says in error.message
    assert error.extra == {"file": SAMPLE_NAME, "page": 1, "row": row}


@pytest.mark.parametrize("layout", DEVELOPED_LAYOUTS)
def test_a_page_that_does_not_open_as_an_advice_has_no_recognisable_header(layout: str) -> None:
    error = refusal(swapped(pages_of(rendered(layout)), "REMITTANCE ADVICE", "STATEMENT"))
    assert NO_HEADER in error.message
    assert error.extra == {"file": SAMPLE_NAME, "page": 1, "row": 1}


@pytest.mark.parametrize(
    ("layout", "old", "new"),
    [
        ("A", "From:", "Payer"),
        ("A", "Payment date:", "Dated:"),
        ("B", "PAYER", "BILL TO"),
        ("C", "ACME WIDGETS LTD   PAY-2024-0088", "ACME WIDGETS LTD PAY-2024-0088"),
    ],
)
def test_a_header_in_no_form_this_reader_knows_is_refused(layout: str, old: str, new: str) -> None:
    error = refusal(swapped(pages_of(rendered(layout)), old, new))
    assert NO_HEADER in error.message
    assert error.extra["page"] == 1


def test_a_labelled_header_must_have_every_label_once() -> None:
    pages = pages_of(rendered("A"))
    missing = refusal(without_row(pages, "Payment date:"))
    assert f"{NO_HEADER}: no Payment date:" in missing.message
    twice = refusal(swapped(pages, "Payment date:", "Payment reference:"))
    assert "'Payment reference:' appears twice" in twice.message


def test_a_payer_column_must_be_a_name_a_reference_and_a_date() -> None:
    error = refusal(without_row(pages_of(rendered("B")), "Ref PAY-2024-0088"))
    assert f"{NO_HEADER}: the payer column holds 2 entries" in error.message


def test_a_page_with_no_table_heading_is_refused() -> None:
    for layout in DEVELOPED_LAYOUTS:
        error = refusal(swapped(pages_of(rendered(layout)), "Invoice", "Document"))
        assert "no line-item table" in error.message
        assert error.extra == {"file": SAMPLE_NAME, "page": 1, "row": 0}


def test_a_table_must_be_closed_by_a_total_and_hold_a_line() -> None:
    pages = pages_of(rendered("A", narrative=None))
    assert "not closed by a total" in refusal(without_row(pages, "TOTAL")).message
    one_line = pages_of(rendered("A", lines=[RemitLine("INV-2024-0891", 441_700)], narrative=None))
    assert "holds no line items" in refusal(without_row(one_line, "INV-2024-0891")).message


def test_a_line_must_fill_the_columns_the_table_has() -> None:
    pages = pages_of(rendered("B"))
    # Take the balance off one line: the table has a Balance column, so the line is short.
    row = next(row for row in pages[0].rows if "1233.50" in row.texts)
    short = tuple(run for run in row.runs if run.text != "1233.50")
    edited = tuple(
        type(page)(
            number=page.number,
            rows=tuple(
                type(row)(number=other.number, runs=short) if other is row else other
                for other in page.rows
            ),
        )
        for page in pages
    )
    assert "a line item with nothing under 'Balance'" in refusal(edited).message


def test_text_that_sits_under_no_column_is_not_read_around() -> None:
    pages = pages_of(rendered("A"))
    row = next(row for row in pages[0].rows if "4417.00" in row.texts)
    stray = type(row.runs[0])(text="approved JB", x0=50_000, x1=55_000, top=row.runs[0].top)
    edited = tuple(
        type(page)(
            number=page.number,
            rows=tuple(
                type(other)(number=other.number, runs=(*other.runs, stray))
                if other is row
                else other
                for other in page.rows
            ),
        )
        for page in pages
    )
    error = refusal(edited)
    assert "'approved JB' sits under no column heading" in error.message
    assert error.extra["row"] == row.number


def test_text_beside_the_paragraph_is_not_read_around() -> None:
    pages = pages_of(rendered("A"))
    last = pages[0].rows[-1]
    stray = type(last.runs[0])(text="Page 1", x0=50_000, x1=53_000, top=last.runs[0].top)
    edited = (
        type(pages[0])(
            number=1,
            rows=(*pages[0].rows[:-1], type(last)(number=last.number, runs=(*last.runs, stray))),
        ),
    )
    assert "text after the total that is not prose" in refusal(edited).message


# -- more than one page ----------------------------------------------------------------------


def long_advice() -> tuple[Page, ...]:
    lines = [RemitLine(f"INV-2024-{number:04d}", 10_000 + number) for number in range(30)]
    return pages_of(rendered("C", lines=lines))


def test_a_page_missing_from_a_long_advice_is_noticed() -> None:
    first, second = long_advice()
    alone = refusal((first,))
    assert "says it is page 1 of 2; it is page 1 of 1" in alone.message
    orphan = refusal((second,))
    assert "says it is page 2 of 2; it is page 1 of 1" in orphan.message
    assert "says it is page 2 of 2; it is page 1 of 2" in refusal((second, first)).message


def test_pages_of_two_different_advices_are_not_one_advice() -> None:
    first, second = long_advice()
    other = swapped(
        (second,),
        "ACME WIDGETS LTD   PAY-2024-0088",
        "ACME WIDGETS LTD   PAY-2024-0089",
        page=2,
    )
    error = refusal((first, *other))
    assert "not a continuation of the advice on page 1" in error.message
    assert error.extra["page"] == 2


def test_a_continued_page_holds_nothing_after_saying_so() -> None:
    first, second = long_advice()
    extra = type(first.rows[-1])(
        number=first.rows[-1].number + 1,
        runs=(type(first.rows[0].runs[0])(text="PTO", x0=4_200, x1=6_000, top=80_000),),
    )
    edited = type(first)(number=1, rows=(*first.rows, extra))
    assert "text after 'continued overleaf'" in refusal((edited, second)).message


def test_one_of_several_pages_must_say_which() -> None:
    first, second = long_advice()
    unmarked = without_marker(first)
    assert "does not say which" in refusal((unmarked, second)).message


def without_marker(page: Page) -> Page:
    rows = tuple(
        type(row)(number=row.number, runs=tuple(run for run in row.runs if "Page " not in run.text))
        for row in page.rows
    )
    return type(page)(number=page.number, rows=rows)

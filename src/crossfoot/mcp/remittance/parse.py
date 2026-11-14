"""Text runs to a remittance advice. Structural, strict, and whole: the advice or nothing.

An advice is a form, and it is read as one. There is a title, a header that names the payer and
the payment, a table whose columns are named by its heading row, a total, and sometimes a
paragraph. Each run on the page is placed in one of those regions by where it sits; none is
skipped and none is guessed at. A line item is never read apart from the header above it, which
is the difference between this and cutting the page into passages of text (plan §3.5).

**Developed against three layouts, and it does not know their names.** Senders lay the same
form out differently, so the reader keys on what the form says rather than on which sender drew
it, and every tolerance below is one a developed layout needs:

* *The header comes in three forms.* Labelled rows (`From:`, `Payment reference:`, `Payment
  date:`, `Total:`); a `PAYER` column beside a `PAYEE` one; or the payer and reference on one
  line with `Paid <date>` under it. Exactly one must be present.
* *Columns are found by their headings, not their order.* The heading row is the one made only
  of `Invoice`, `Amount`, `Balance` and `Note`, and a cell belongs to the heading it sits under
  -- its span overlaps that heading's -- so amount-first tables and right-aligned amounts read
  the same as the rest.
* *The total closes the table* as a `TOTAL` row or a `PAID <amount>` line, and the paragraph, if
  any, is whatever follows it. A table that runs over a page says `continued overleaf` and each
  page says `Page n of m`.
* *The currency is a symbol*, printed on the total, on every amount, or both. Wherever it
  appears it must be the same one, and it must appear somewhere.

**Strict wherever a mistake could move money or an id**, as `bank/parse.py` is. An amount is
digits, a point and two digits -- `1,250.00` is a different sender, not a rounding choice. A
line's reference must have the shape of a ledger reference. The lines must add up to the stated
total; a second statement of the total must agree with the first; a running balance must follow
from the line above. A page with text this reader has no place for is refused rather than read
around: what it cannot account for it does not understand.

**What is read is what the payer wrote.** A line amount is never compared with an invoice here.
Some advices differ from the ledger by pence on purpose (spec 01 §4.8), and whether that matters
is for the matcher, with both numbers in front of it.

A page that will not read is `malformed_source`, naming the file, the page and the row.

Nothing here reads `remit.jsonl` or a layout id -- there is none on the page to read. This is
also deliberately not a wrapper around the generator's own records: the test suite grades this
reader against the generator's truth, which it could not do if they shared code.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError
from crossfoot.mcp.remittance.files import PAYMENT_REF
from crossfoot.mcp.remittance.layout import Page, Row, Run

TITLE: Final = "REMITTANCE ADVICE"

INVOICE: Final = "Invoice"
AMOUNT: Final = "Amount"
BALANCE: Final = "Balance"
NOTE: Final = "Note"
HEADINGS: Final = (INVOICE, AMOUNT, BALANCE, NOTE)

LABEL_PAYER: Final = "From:"
LABEL_REF: Final = "Payment reference:"
LABEL_DATE: Final = "Payment date:"
LABEL_TOTAL: Final = "Total:"
LABELS: Final = (LABEL_PAYER, LABEL_REF, LABEL_DATE, LABEL_TOTAL)

PAYER_HEADING: Final = "PAYER"
PAYEE_HEADING: Final = "PAYEE"
TOTAL_MARK: Final = "TOTAL"
CONTINUED: Final = "continued overleaf"

#: The symbols the developed layouts print. Which currency each one means is `canonical.py`'s.
SYMBOLS: Final = "£$€"
#: Two decimal places, always, and no separators.
_AMOUNT: Final = re.compile(rf"^(-?)([{re.escape(SYMBOLS)}]?)(\d+)\.(\d{{2}})$")
_ISO_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: `INV-2024-0891` or `CN-2024-0007` (spec 01 §5.3, without the source prefix).
LEDGER_REF: Final = re.compile(r"^(?:INV|CN)-\d{4}-\d{4,}$")

_INLINE: Final = re.compile(r"^(\S(?:.*\S)?) {2,}(\S+)$")
_PAID_ON: Final = re.compile(r"^Paid (\S+)$")
_PAGE_OF: Final = re.compile(r"^Page (\d+) of (\d+)$")
_REF_ROW: Final = re.compile(r"^Ref (\S+)$")
_PAID_TOTAL: Final = re.compile(r"^PAID (\S+)$")

NO_HEADER: Final = "no recognisable header"


@dataclass(frozen=True, slots=True)
class Amount:
    """An amount as printed: integer minor units, and the symbol beside it ("" if none)."""

    minor: int
    symbol: str


@dataclass(frozen=True, slots=True)
class Line:
    """One line item, as the advice states it."""

    invoice_ref: str
    amount_minor: int
    note: str | None


@dataclass(frozen=True, slots=True)
class Advice:
    """One remittance advice, whole. Amounts are integer minor units of `symbol`'s currency."""

    payer: str
    payment_ref: str
    paid_on: date
    symbol: str
    total_minor: int
    lines: tuple[Line, ...]
    narrative: str | None
    pages: int


@dataclass(frozen=True, slots=True)
class _Header:
    payer: str
    payment_ref: str
    paid_on: date
    total: Amount | None
    page_of: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class _Item:
    """A line with where it was read from, and its running balance when the table has one."""

    line: Line
    symbol: str
    balance: Amount | None
    page: int
    row: int


@dataclass(frozen=True, slots=True)
class _Part:
    """What one page holds."""

    number: int
    header: _Header
    headings: tuple[str, ...]
    items: tuple[_Item, ...]
    total: Amount | None
    continued: bool
    narrative: tuple[str, ...]


def parse_amount(text: str) -> Amount | None:
    """`-£892.79` -> -89279 and `£`. Integers from digits; no binary fraction is ever built."""
    match = _AMOUNT.match(text)
    if match is None:
        return None
    minor = int(match.group(3)) * 100 + int(match.group(4))
    return Amount(minor=-minor if match.group(1) else minor, symbol=match.group(2))


def _overlaps(run: Run, heading: Run) -> bool:
    return run.x0 < heading.x1 and heading.x0 < run.x1


class _Reader:
    """One file's reading. Holds the name so every refusal can say where it is."""

    def __init__(self, name: str) -> None:
        self.name = name

    def bad(self, page: int, row: int, why: str) -> ToolError:
        where = f"{self.name} page {page}" + (f" row {row}" if row else "")
        return ToolError(MALFORMED_SOURCE, f"{where}: {why}", file=self.name, page=page, row=row)

    # -- small readers ----------------------------------------------------------------------

    def amount(self, page: int, row: int, text: str, what: str) -> Amount:
        parsed = parse_amount(text)
        if parsed is None:
            raise self.bad(page, row, f"{what} {text!r} is not an amount with two decimal places")
        return parsed

    def day(self, page: int, row: int, text: str) -> date:
        if _ISO_DATE.match(text) is None:
            raise self.bad(page, row, f"{text!r} is not a YYYY-MM-DD date")
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise self.bad(page, row, f"{text!r} is not a real date") from exc

    def payment_ref(self, page: int, row: int, text: str) -> str:
        if PAYMENT_REF.match(text) is None:
            raise self.bad(page, row, f"{text!r} is not a payment reference (PAY-YYYY-NNNN)")
        return text

    def column(self, page: int, row: Row, run: Run, headings: Sequence[Run]) -> str:
        """The heading a run sits under: the one whose span it overlaps, and only one."""
        under = [heading.text for heading in headings if _overlaps(run, heading)]
        if len(under) != 1:
            problem = "sits under no column heading" if not under else "spans two columns"
            raise self.bad(page, row.number, f"{run.text!r} {problem}")
        return under[0]

    def cells(self, page: int, row: Row, headings: Sequence[Run]) -> dict[str, str]:
        found: dict[str, str] = {}
        for run in row.runs:
            name = self.column(page, row, run, headings)
            if name in found:
                raise self.bad(page, row.number, f"two entries under {name!r}")
            found[name] = run.text
        return found

    # -- the header -------------------------------------------------------------------------

    def header(self, page: int, rows: Sequence[Row]) -> _Header:
        if not rows:
            raise self.bad(page, 0, f"{NO_HEADER}: nothing between the title and the table")
        first = rows[0]
        forms = {
            "labelled": first.runs[0].text.endswith(":"),
            "columns": first.texts == (PAYER_HEADING, PAYEE_HEADING),
            "inline": len(first.runs) == 1 and _INLINE.match(first.runs[0].text) is not None,
        }
        present = [form for form, seen in forms.items() if seen]
        if len(present) != 1:
            raise self.bad(
                page,
                first.number,
                f"{NO_HEADER}: {first.runs[0].text!r} does not open any header this reader knows",
            )
        if present[0] == "labelled":
            return self._labelled(page, rows)
        if present[0] == "columns":
            return self._two_columns(page, rows)
        return self._inline(page, rows)

    def _labelled(self, page: int, rows: Sequence[Row]) -> _Header:
        values: dict[str, tuple[str, int]] = {}
        for row in rows:
            if len(row.runs) != 2 or row.runs[0].text not in LABELS:
                raise self.bad(
                    page,
                    row.number,
                    f"{NO_HEADER}: expected a label and its value, one of {', '.join(LABELS)}",
                )
            label = row.runs[0].text
            if label in values:
                raise self.bad(page, row.number, f"{NO_HEADER}: {label!r} appears twice")
            values[label] = (row.runs[1].text, row.number)
        missing = [label for label in LABELS if label not in values]
        if missing:
            raise self.bad(page, rows[0].number, f"{NO_HEADER}: no {', '.join(missing)}")
        return _Header(
            payer=values[LABEL_PAYER][0],
            payment_ref=self.payment_ref(page, values[LABEL_REF][1], values[LABEL_REF][0]),
            paid_on=self.day(page, values[LABEL_DATE][1], values[LABEL_DATE][0]),
            total=self.amount(page, values[LABEL_TOTAL][1], values[LABEL_TOTAL][0], "the total"),
            page_of=None,
        )

    def _two_columns(self, page: int, rows: Sequence[Row]) -> _Header:
        headings = rows[0].runs
        payer_side: list[tuple[str, int]] = []
        for row in rows[1:]:
            for run in row.runs:
                if self.column(page, row, run, headings) == PAYER_HEADING:
                    payer_side.append((run.text, row.number))
        # The payee side is this business's own name and address: on the page, and not a fact
        # about the payment. It is accounted for by being placed under its heading.
        if len(payer_side) != 3:
            raise self.bad(
                page,
                rows[0].number,
                f"{NO_HEADER}: the payer column holds {len(payer_side)} entries, not a name, "
                "a reference and a date",
            )
        (payer, _), (ref_text, ref_row), (day_text, day_row) = payer_side
        ref = _REF_ROW.match(ref_text)
        if ref is None:
            raise self.bad(page, ref_row, f"{NO_HEADER}: {ref_text!r} is not `Ref <reference>`")
        return _Header(
            payer=payer,
            payment_ref=self.payment_ref(page, ref_row, ref.group(1)),
            paid_on=self.day(page, day_row, day_text),
            total=None,
            page_of=None,
        )

    def _inline(self, page: int, rows: Sequence[Row]) -> _Header:
        if len(rows) != 2 or len(rows[1].runs) not in (1, 2):
            raise self.bad(
                page,
                rows[0].number,
                f"{NO_HEADER}: expected the payer and reference on one line and the payment "
                "date on the next",
            )
        opening = _INLINE.match(rows[0].runs[0].text)
        assert opening is not None  # it is how this form was recognised
        paid = _PAID_ON.match(rows[1].runs[0].text)
        if paid is None:
            raise self.bad(
                page, rows[1].number, f"{NO_HEADER}: {rows[1].runs[0].text!r} is not `Paid <date>`"
            )
        page_of: tuple[int, int] | None = None
        if len(rows[1].runs) == 2:
            marker = _PAGE_OF.match(rows[1].runs[1].text)
            if marker is None:
                raise self.bad(
                    page, rows[1].number, f"{rows[1].runs[1].text!r} is not `Page n of m`"
                )
            page_of = (int(marker.group(1)), int(marker.group(2)))
        return _Header(
            payer=opening.group(1),
            payment_ref=self.payment_ref(page, rows[0].number, opening.group(2)),
            paid_on=self.day(page, rows[1].number, paid.group(1)),
            total=None,
            page_of=page_of,
        )

    # -- the table --------------------------------------------------------------------------

    @staticmethod
    def is_heading_row(row: Row) -> bool:
        texts = row.texts
        return (
            INVOICE in texts
            and AMOUNT in texts
            and all(text in HEADINGS for text in texts)
            and len(set(texts)) == len(texts)
        )

    def closing(self, page: int, row: Row, headings: Sequence[Run]) -> Amount | None:
        """The total a row closes the table with, or None when the row is a line item."""
        if len(row.runs) == 1:
            paid = _PAID_TOTAL.match(row.runs[0].text)
            if paid is not None:
                return self.amount(page, row.number, paid.group(1), "the amount paid")
        if TOTAL_MARK in row.texts:
            cells = self.cells(page, row, headings)
            if cells.get(INVOICE) != TOTAL_MARK or set(cells) != {INVOICE, AMOUNT}:
                raise self.bad(page, row.number, "a total row holds TOTAL and one amount")
            return self.amount(page, row.number, cells[AMOUNT], "the total")
        return None

    def item(self, page: int, row: Row, headings: Sequence[Run]) -> _Item:
        cells = self.cells(page, row, headings)
        names = [heading.text for heading in headings]
        for required in (INVOICE, AMOUNT, *([BALANCE] if BALANCE in names else [])):
            if required not in cells:
                raise self.bad(page, row.number, f"a line item with nothing under {required!r}")
        ref = cells[INVOICE]
        if LEDGER_REF.match(ref) is None:
            raise self.bad(page, row.number, f"{ref!r} is not an invoice or credit-note reference")
        amount = self.amount(page, row.number, cells[AMOUNT], "the amount")
        balance = (
            self.amount(page, row.number, cells[BALANCE], "the balance")
            if BALANCE in cells
            else None
        )
        return _Item(
            line=Line(invoice_ref=ref, amount_minor=amount.minor, note=cells.get(NOTE)),
            symbol=amount.symbol,
            balance=balance,
            page=page,
            row=row.number,
        )

    # -- one page, then all of them ---------------------------------------------------------

    def part(self, page: Page) -> _Part:
        rows = page.rows
        if not rows or rows[0].texts != (TITLE,):
            raise self.bad(
                page.number,
                1 if rows else 0,
                f"{NO_HEADER}: the page does not begin with the title {TITLE!r}",
            )
        table_at = next(
            (index for index, row in enumerate(rows) if index and self.is_heading_row(row)), None
        )
        if table_at is None:
            raise self.bad(
                page.number,
                0,
                "no line-item table: no row of column headings "
                f"({INVOICE} and {AMOUNT}, with {BALANCE} or {NOTE})",
            )
        header = self.header(page.number, rows[1:table_at])
        headings = rows[table_at].runs

        items: list[_Item] = []
        total: Amount | None = None
        continued = False
        rest: Sequence[Row] = ()
        for index in range(table_at + 1, len(rows)):
            row = rows[index]
            if row.texts == (CONTINUED,):
                continued, rest = True, rows[index + 1 :]
                break
            total = self.closing(page.number, row, headings)
            if total is not None:
                rest = rows[index + 1 :]
                break
            items.append(self.item(page.number, row, headings))
        else:
            raise self.bad(page.number, 0, "the table is not closed by a total")
        if not items:
            raise self.bad(page.number, rows[table_at].number, "the table holds no line items")

        if continued and rest:
            raise self.bad(page.number, rest[0].number, f"text after {CONTINUED!r}")
        for row in rest:
            # A paragraph line is one run. Two side by side is a region this reader cannot name.
            if len(row.runs) != 1:
                raise self.bad(page.number, row.number, "text after the total that is not prose")
        return _Part(
            number=page.number,
            header=header,
            headings=tuple(heading.text for heading in headings),
            items=tuple(items),
            total=total,
            continued=continued,
            narrative=tuple(row.runs[0].text for row in rest),
        )

    def advice(self, pages: Sequence[Page]) -> Advice:
        parts = [self.part(page) for page in pages]
        first, last = parts[0], parts[-1]
        for index, part in enumerate(parts, 1):
            same = (part.header.payer, part.header.payment_ref, part.header.paid_on) == (
                first.header.payer,
                first.header.payment_ref,
                first.header.paid_on,
            )
            if not same or part.headings != first.headings:
                raise self.bad(part.number, 0, "not a continuation of the advice on page 1")
            marker = part.header.page_of
            if marker is None and len(parts) > 1:
                raise self.bad(part.number, 0, "one of several pages, and it does not say which")
            if marker is not None and marker != (index, len(parts)):
                raise self.bad(
                    part.number,
                    0,
                    f"says it is page {marker[0]} of {marker[1]}; "
                    f"it is page {index} of {len(parts)}",
                )
            if part.continued and part is last:
                raise self.bad(part.number, 0, f"says {CONTINUED!r} and there is no next page")
            if not part.continued and part is not last:
                raise self.bad(part.number, 0, "closes the table, and more pages follow")

        items = [item for part in parts for item in part.items]
        assert last.total is not None  # the last page did not say "continued", so it closed
        stated = [last.total] + ([first.header.total] if first.header.total is not None else [])
        if len({amount.minor for amount in stated}) != 1:
            raise self.bad(
                last.number,
                0,
                "states two different totals: "
                + " and ".join(str(amount.minor) for amount in stated)
                + " minor units",
            )
        total = stated[0].minor

        symbols = {amount.symbol for amount in stated} | {item.symbol for item in items}
        symbols |= {item.balance.symbol for item in items if item.balance is not None}
        symbols.discard("")
        if len(symbols) != 1:
            raise self.bad(
                last.number,
                0,
                "no currency symbol on any amount"
                if not symbols
                else f"amounts in more than one currency ({' '.join(sorted(symbols))})",
            )

        summed = sum(item.line.amount_minor for item in items)
        if summed != total:
            raise self.bad(
                last.number,
                0,
                f"the lines add up to {summed} and the advice states a total of {total} "
                "minor units",
            )
        remaining = total
        for item in items:
            remaining -= item.line.amount_minor
            if item.balance is not None and item.balance.minor != remaining:
                raise self.bad(
                    item.page,
                    item.row,
                    f"the running balance states {item.balance.minor} where {remaining} "
                    "minor units remain",
                )

        return Advice(
            payer=first.header.payer,
            payment_ref=first.header.payment_ref,
            paid_on=first.header.paid_on,
            symbol=symbols.pop(),
            total_minor=total,
            lines=tuple(item.line for item in items),
            narrative=" ".join(last.narrative) or None,
            pages=len(parts),
        )


def parse_advice(name: str, pages: Sequence[Page]) -> Advice:
    """The advice a PDF's pages hold, or `malformed_source` saying where it stopped making sense."""
    return _Reader(name).advice(pages)

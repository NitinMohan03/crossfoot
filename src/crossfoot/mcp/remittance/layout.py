"""A PDF's bytes to text runs with positions. The only module that knows the PDF library.

**The unit is the text run, not the word and not the chunk.** A run is a stretch of characters
with no horizontal gap in it -- in practice one string the sender's software drew: a label, a
value, one table cell, one line of a paragraph. Spaces inside a run are kept (`KINGSMEAD TRADING
LLP` is one run), and a gap wider than a space ends it. Runs sharing a baseline form a row. That
is the whole geometry `parse.py` needs to read an advice as the form it is -- which cell sits
under which column header, which value sits beside which label -- and it is exactly what
splitting the page into passages of text would throw away (plan §3.5: a line item means nothing
without the header naming the payer).

Positions are integers, hundredths of a point from the page's left and top edges. Nothing here
is money; amounts are still text when they leave this module.

**The library is handed bytes in memory and never a path**, so it cannot open anything: the
package's one file handle is in `files.py`. And it is called in exactly one place, inside one
`try`. pdfminer signals a damaged file with whatever exception the damaged part happens to
raise -- a syntax error, a key error, a bad cast -- so the boundary catches them all and answers
`malformed_source`, naming the file and the kind of failure. A broad `except` is wrong almost
everywhere; at the edge of a parser reading untrusted bytes it is the point.
"""

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Final

from pdfplumber.pdf import PDF

from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError

#: Every PDF begins with this. A file that does not is not handed to the library at all.
MAGIC: Final = b"%PDF-"
#: Points. A gap wider than this between two characters ends a run; a space is narrower.
RUN_GAP: Final = 3
#: Hundredths of a point. Runs whose tops differ by no more than this share a row. Rows on the
#: developed layouts are never closer than 11 points.
ROW_TOLERANCE: Final = 200
SCALE: Final = 100
#: What pdfminer writes for a glyph its font gives no Unicode value for.
UNMAPPED_GLYPH: Final = "(cid:"


@dataclass(frozen=True, slots=True)
class Run:
    """One drawn string and where it sits. Positions are hundredths of a point."""

    text: str
    x0: int
    x1: int
    top: int


@dataclass(frozen=True, slots=True)
class Row:
    """The runs sharing one baseline, left to right. `number` is 1-based within the page."""

    number: int
    runs: tuple[Run, ...]

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(run.text for run in self.runs)


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    rows: tuple[Row, ...]


def _hundredths(value: Any) -> int:
    scaled: int = round(value * SCALE)
    return scaled


def _rows(words: list[dict[str, Any]]) -> tuple[Row, ...]:
    runs = sorted(
        (
            Run(
                text=str(word["text"]),
                x0=_hundredths(word["x0"]),
                x1=_hundredths(word["x1"]),
                top=_hundredths(word["top"]),
            )
            for word in words
            if str(word["text"]).strip()
        ),
        key=lambda run: (run.top, run.x0),
    )
    grouped: list[list[Run]] = []
    for run in runs:
        if grouped and run.top - grouped[-1][0].top <= ROW_TOLERANCE:
            grouped[-1].append(run)
        else:
            grouped.append([run])
    return tuple(
        Row(number=number, runs=tuple(sorted(members, key=lambda run: run.x0)))
        for number, members in enumerate(grouped, 1)
    )


def read_pages(name: str, raw: bytes, *, max_pages: int) -> tuple[Page, ...]:
    """Every page of a PDF as rows of text runs, or `malformed_source` naming the file."""

    def bad(why: str) -> ToolError:
        return ToolError(MALFORMED_SOURCE, f"{name}: {why}", file=name)

    if not raw:
        raise bad("the file is empty")
    if not raw.startswith(MAGIC):
        raise bad("not a PDF (it does not begin with a PDF header)")

    pages: list[Page] = []
    try:
        with PDF(BytesIO(raw)) as document:
            count = len(document.pages)
            if count > max_pages:
                raise bad(f"{count} pages, more than the {max_pages} any advice runs to")
            for sheet in document.pages:
                words = sheet.extract_words(
                    keep_blank_chars=True, x_tolerance=RUN_GAP, y_tolerance=RUN_GAP
                )
                pages.append(Page(number=sheet.page_number, rows=_rows(words)))
    except ToolError:
        raise
    except Exception as exc:
        # The one deliberate catch-all in the package: see the module docstring.
        raise bad(f"the PDF could not be read ({type(exc).__name__})") from exc

    if not pages:
        raise bad("the PDF has no pages")
    for page in pages:
        for row in page.rows:
            if any(UNMAPPED_GLYPH in text for text in row.texts):
                raise ToolError(
                    MALFORMED_SOURCE,
                    f"{name} page {page.number} row {row.number}: text that does not decode "
                    "(a glyph with no character behind it)",
                    file=name,
                    page=page.number,
                    row=row.number,
                )
    return tuple(pages)

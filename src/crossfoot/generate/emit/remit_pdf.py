"""The four remittance layouts (spec 01 §6).

A payer always uses the same layout, chosen once per counterparty in `world.py` with weights
30/30/25/15, so a parser cannot learn the layout from the payment.

    A  portrait, header block top-left, one table, totals row
    B  portrait, two-column header, running-balance column, narrative in the footer
    C  portrait, multi-page past `ROWS_PER_PAGE`, amount-first columns, currency symbols
    D  landscape, narrative *before* the table, refs as `0891/2024`, thousands separators

D is `config.HELD_OUT_TEMPLATE`: the Phase 2 parser is never developed against it and its parse
rate is reported at the end. Nothing here treats it differently -- holding it out is a rule
about who may read the file, enforced in the parser, not a property of the bytes.

**Determinism** (spec 01 §9). Two things make the bytes stable, and both were established by
measurement rather than assumed:

  * `rl_config.invariant`, set at import before any canvas exists, pins `/CreationDate` and
    `/ModDate` to `D:20000101000000` and derives the `/ID` array from a digest of the document
    content rather than from random bytes. `/ID` is still written -- it is simply the same for
    the same input -- so two runs in separate processes agree byte for byte.
  * `setProducer(PRODUCER)` overrides the one remaining version-dependent field. reportlab
    4.2.5 writes `/Producer (ReportLab PDF Library - www.reportlab.com)` and 5.1.0 writes a
    different string, so without this a reportlab upgrade would silently rewrite every
    committed golden PDF while changing nothing anyone asked for.

Only the built-in Type 1 fonts are used, so no font file or subset is embedded.
"""

import io
from typing import Final

from reportlab import rl_config

# Must happen before the first canvas is constructed.
rl_config.invariant = 1
rl_config.pageCompression = 0

from reportlab.lib.pagesizes import A4, landscape  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from crossfoot.generate.records import RemitLine, Remittance  # noqa: E402
from crossfoot.money import format_minor  # noqa: E402

# Our own fixed `/Producer` and `/Creator`. Both default to version-dependent strings, so
# setting them keeps those two fields out of the version's hands (see the module docstring).
PRODUCER: Final = "crossfoot remittance writer"
CREATOR: Final = "crossfoot"

PAYEE_NAME: Final = "CROSSFOOT TRADING LIMITED"
PAYEE_ADDRESS: Final = ("17 Lombard Walk", "London EC2N 4AY", "United Kingdom")

BODY_FONT: Final = "Helvetica"
BOLD_FONT: Final = "Helvetica-Bold"
MONO_FONT: Final = "Courier"
TITLE_SIZE: Final = 15
BODY_SIZE: Final = 9
SMALL_SIZE: Final = 8

MARGIN: Final = 42
LINE: Final = 13
# Layout C paginates past this many rows. Current group sizes never reach it -- the source cap
# is four ids (§4.6) -- so this is the path a wider dataset would take, kept correct rather
# than left to rot.
ROWS_PER_PAGE: Final = 25

CURRENCY_SYMBOL: Final = {"GBP": "£", "USD": "$", "EUR": "€"}


def money(amount_minor: int, *, currency: str | None = None, grouped: bool = False) -> str:
    """A 2dp amount string, optionally with a currency symbol and thousands separators."""
    text = format_minor(abs(amount_minor))
    if grouped:
        whole, _, part = text.partition(".")
        text = f"{int(whole):,}.{part}"
    symbol = CURRENCY_SYMBOL.get(currency or "", "") if currency else ""
    sign = "-" if amount_minor < 0 else ""
    return f"{sign}{symbol}{text}"


def plain_ref(line: RemitLine) -> str:
    return line.invoice_ref


def slashed_ref(line: RemitLine) -> str:
    """`INV-2024-0891` -> `0891/2024`, the way template D writes a reference (§6)."""
    parts = line.invoice_ref.split("-")
    if len(parts) != 3:
        return line.invoice_ref
    kind, year, number = parts
    stem = f"{number}/{year}"
    return stem if kind == "INV" else f"{kind} {stem}"


def _wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap. No hyphenation, so the result depends only on the input."""
    out: list[str] = []
    current: list[str] = []
    for word in text.split(" "):
        candidate = " ".join([*current, word])
        if current and len(candidate) > width:
            out.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        out.append(" ".join(current))
    return out


def _new_canvas(stream: io.BytesIO, *, wide: bool = False) -> canvas.Canvas:
    pagesize = landscape(A4) if wide else A4
    pdf = canvas.Canvas(stream, pagesize=pagesize, invariant=1, pageCompression=0)
    pdf.setProducer(PRODUCER)
    pdf.setCreator(CREATOR)
    return pdf


def _title(pdf: canvas.Canvas, top: float, advice: Remittance) -> float:
    pdf.setFont(BOLD_FONT, TITLE_SIZE)
    pdf.drawString(MARGIN, top, "REMITTANCE ADVICE")
    return top - LINE * 2


def _narrative_block(pdf: canvas.Canvas, top: float, advice: Remittance, width: int) -> float:
    if not advice.narrative:
        return top
    pdf.setFont(BODY_FONT, SMALL_SIZE)
    for row in _wrap(advice.narrative, width):
        pdf.drawString(MARGIN, top, row)
        top -= LINE - 2
    return top - LINE


def _template_a(advice: Remittance) -> bytes:
    stream = io.BytesIO()
    pdf = _new_canvas(stream)
    _, height = A4
    top = _title(pdf, height - MARGIN, advice)

    pdf.setFont(BODY_FONT, BODY_SIZE)
    for label, value in (
        ("From", advice.payer),
        ("Payment reference", advice.payment_ref),
        ("Payment date", advice.paid_on.isoformat()),
        ("Total", money(advice.total_minor, currency=advice.currency)),
    ):
        pdf.drawString(MARGIN, top, f"{label}:")
        pdf.drawString(MARGIN + 110, top, value)
        top -= LINE
    top -= LINE

    pdf.setFont(BOLD_FONT, BODY_SIZE)
    pdf.drawString(MARGIN, top, "Invoice")
    pdf.drawString(MARGIN + 130, top, "Amount")
    pdf.drawString(MARGIN + 230, top, "Note")
    top -= LINE * 0.4
    pdf.line(MARGIN, top, MARGIN + 460, top)
    top -= LINE

    pdf.setFont(BODY_FONT, BODY_SIZE)
    for line in advice.lines:
        pdf.drawString(MARGIN, top, plain_ref(line))
        pdf.drawString(MARGIN + 130, top, money(line.amount_minor))
        if line.note:
            pdf.drawString(MARGIN + 230, top, line.note)
        top -= LINE

    top -= LINE * 0.4
    pdf.line(MARGIN, top, MARGIN + 460, top)
    top -= LINE
    pdf.setFont(BOLD_FONT, BODY_SIZE)
    pdf.drawString(MARGIN, top, "TOTAL")
    pdf.drawString(MARGIN + 130, top, money(advice.total_minor))
    top -= LINE * 2

    _narrative_block(pdf, top, advice, 92)
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


def _template_b(advice: Remittance) -> bytes:
    stream = io.BytesIO()
    pdf = _new_canvas(stream)
    width, height = A4
    top = _title(pdf, height - MARGIN, advice)
    right = width / 2 + 20

    pdf.setFont(BOLD_FONT, SMALL_SIZE)
    pdf.drawString(MARGIN, top, "PAYER")
    pdf.drawString(right, top, "PAYEE")
    cursor = top - LINE

    pdf.setFont(BODY_FONT, BODY_SIZE)
    payer_rows = (advice.payer, f"Ref {advice.payment_ref}", advice.paid_on.isoformat())
    payee_rows = (PAYEE_NAME, *PAYEE_ADDRESS)
    for index in range(max(len(payer_rows), len(payee_rows))):
        if index < len(payer_rows):
            pdf.drawString(MARGIN, cursor, payer_rows[index])
        if index < len(payee_rows):
            pdf.drawString(right, cursor, payee_rows[index])
        cursor -= LINE
    cursor -= LINE

    pdf.setFont(BOLD_FONT, BODY_SIZE)
    pdf.drawString(MARGIN, cursor, "Invoice")
    pdf.drawString(MARGIN + 130, cursor, "Amount")
    pdf.drawString(MARGIN + 230, cursor, "Balance")
    pdf.drawString(MARGIN + 330, cursor, "Note")
    cursor -= LINE

    # A running balance down to zero, which is how this layout shows the payment closing out.
    pdf.setFont(BODY_FONT, BODY_SIZE)
    balance = advice.total_minor
    for line in advice.lines:
        balance -= line.amount_minor
        pdf.drawString(MARGIN, cursor, plain_ref(line))
        pdf.drawString(MARGIN + 130, cursor, money(line.amount_minor))
        pdf.drawString(MARGIN + 230, cursor, money(balance))
        if line.note:
            pdf.drawString(MARGIN + 330, cursor, line.note)
        cursor -= LINE

    cursor -= LINE
    pdf.setFont(BOLD_FONT, BODY_SIZE)
    pdf.drawString(MARGIN, cursor, f"PAID {money(advice.total_minor, currency=advice.currency)}")

    _narrative_block(pdf, MARGIN + LINE * 4, advice, 100)
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


def _template_c(advice: Remittance) -> bytes:
    stream = io.BytesIO()
    pdf = _new_canvas(stream)
    _, height = A4
    pages = [
        advice.lines[start : start + ROWS_PER_PAGE]
        for start in range(0, max(len(advice.lines), 1), ROWS_PER_PAGE)
    ]
    for page_number, rows in enumerate(pages, 1):
        top = _title(pdf, height - MARGIN, advice)
        pdf.setFont(BODY_FONT, BODY_SIZE)
        pdf.drawString(MARGIN, top, f"{advice.payer}   {advice.payment_ref}")
        top -= LINE
        pdf.drawString(MARGIN, top, f"Paid {advice.paid_on.isoformat()}")
        if len(pages) > 1:
            pdf.drawString(MARGIN + 300, top, f"Page {page_number} of {len(pages)}")
        top -= LINE * 2

        # Amount first, right-aligned, with the currency symbol.
        pdf.setFont(BOLD_FONT, BODY_SIZE)
        pdf.drawRightString(MARGIN + 110, top, "Amount")
        pdf.drawString(MARGIN + 140, top, "Invoice")
        pdf.drawString(MARGIN + 270, top, "Note")
        top -= LINE

        pdf.setFont(MONO_FONT, BODY_SIZE)
        for line in rows:
            pdf.drawRightString(
                MARGIN + 110, top, money(line.amount_minor, currency=advice.currency)
            )
            pdf.drawString(MARGIN + 140, top, plain_ref(line))
            if line.note:
                pdf.drawString(MARGIN + 270, top, line.note)
            top -= LINE

        if page_number == len(pages):
            top -= LINE
            pdf.setFont(BOLD_FONT, BODY_SIZE)
            pdf.drawRightString(
                MARGIN + 110, top, money(advice.total_minor, currency=advice.currency)
            )
            pdf.drawString(MARGIN + 140, top, "TOTAL")
            top -= LINE * 2
            _narrative_block(pdf, top, advice, 92)
        else:
            pdf.setFont(BODY_FONT, SMALL_SIZE)
            pdf.drawString(MARGIN, top - LINE, "continued overleaf")
        pdf.showPage()
    pdf.save()
    return stream.getvalue()


def _template_d(advice: Remittance) -> bytes:
    stream = io.BytesIO()
    pdf = _new_canvas(stream, wide=True)
    _, height = landscape(A4)
    top = _title(pdf, height - MARGIN, advice)

    pdf.setFont(BODY_FONT, BODY_SIZE)
    pdf.drawString(MARGIN, top, f"{advice.payer}")
    pdf.drawString(MARGIN + 320, top, f"Reference {advice.payment_ref}")
    pdf.drawString(MARGIN + 560, top, advice.paid_on.isoformat())
    top -= LINE * 2

    # The narrative comes first in this layout, which is what makes it awkward to parse.
    top = _narrative_block(pdf, top, advice, 130)

    pdf.setFont(BOLD_FONT, BODY_SIZE)
    pdf.drawString(MARGIN, top, "Ref")
    pdf.drawRightString(MARGIN + 260, top, "Amount")
    pdf.drawString(MARGIN + 300, top, "Note")
    top -= LINE

    pdf.setFont(BODY_FONT, BODY_SIZE)
    for line in advice.lines:
        pdf.drawString(MARGIN, top, slashed_ref(line))
        pdf.drawRightString(
            MARGIN + 260, top, money(line.amount_minor, currency=advice.currency, grouped=True)
        )
        if line.note:
            pdf.drawString(MARGIN + 300, top, line.note)
        top -= LINE

    top -= LINE
    pdf.setFont(BOLD_FONT, BODY_SIZE)
    pdf.drawString(MARGIN, top, "Total")
    pdf.drawRightString(
        MARGIN + 260, top, money(advice.total_minor, currency=advice.currency, grouped=True)
    )
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


LAYOUTS: Final = {
    "A": _template_a,
    "B": _template_b,
    "C": _template_c,
    "D": _template_d,
}


def render(advice: Remittance) -> bytes:
    layout = LAYOUTS.get(advice.template)
    if layout is None:
        raise ValueError(f"unknown remittance template {advice.template!r}")
    return layout(advice)

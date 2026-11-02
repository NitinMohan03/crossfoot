"""How a payer's name and reference appear on the money side (spec 01 §3.2).

`description_for` lives here rather than in `emit/bank.py` so a type builder can assert against
the exact 32-character string the bank will write. The `name` and `reasoning(a)` classes put
their discrepancy *in* that string, so asserting on the pre-truncation surface would be
asserting on something the matcher never sees.
"""

from typing import Final

from crossfoot.generate.records import PaymentLeg
from crossfoot.names import trivially_normalises

DESCRIPTION_MAX: Final = 32
NAME_FLOOR: Final = 12


def truncate_words(text: str, limit: int) -> str:
    """Truncate on a word boundary so a partial token never survives."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] == " ":
        return cut
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip()


def reference_fragment(leg: PaymentLeg, invoice_ref: str) -> str | None:
    """The literal text the payer typed, or None when they quoted nothing."""
    style = leg.reference_style
    if style == "none":
        return None
    if style == "exact":
        return invoice_ref
    digits = invoice_ref.rsplit("-", 1)[-1]
    if style == "compact":
        return f"INV{digits}"
    if style == "digits":
        return digits
    if style == "po":
        return leg.po_number
    raise ValueError(f"unknown reference style {style!r}")


def description_for(leg: PaymentLeg, invoice_ref: str | None) -> str:
    """Payer name, plus whatever reference the payer typed, as a bank would truncate it."""
    name = " ".join(leg.payer_surface.upper().split())
    fragment = reference_fragment(leg, invoice_ref) if invoice_ref else None
    if not fragment:
        return truncate_words(name, DESCRIPTION_MAX)[:DESCRIPTION_MAX]
    budget = DESCRIPTION_MAX - len(fragment) - 1
    if leg.preserve_name:
        # The name is the discrepancy; shortening it would move the similarity the builder
        # asserted. Drop the reference instead.
        if budget < len(name):
            return truncate_words(name, DESCRIPTION_MAX)[:DESCRIPTION_MAX]
        return f"{name} {fragment}"[:DESCRIPTION_MAX]
    short = truncate_words(name, budget)
    if budget < NAME_FLOOR or not trivially_normalises(leg.payer_surface, short):
        # The reference would cost the payer name its identity; a real export would still
        # carry the name, so drop the reference instead (spec 01 §4.1).
        return truncate_words(name, DESCRIPTION_MAX)[:DESCRIPTION_MAX]
    return f"{short} {fragment}"[:DESCRIPTION_MAX]

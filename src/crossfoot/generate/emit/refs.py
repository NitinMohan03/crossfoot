"""How a payer quotes (or mangles) an invoice reference on the money side (spec 01 §3.2)."""

from crossfoot.generate.records import PaymentLeg


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

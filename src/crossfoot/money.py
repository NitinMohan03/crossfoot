"""Money helpers. Minor units are integers; ratios are Decimal. Never float."""

from decimal import ROUND_HALF_UP, Decimal


def round_half_up(value: Decimal) -> int:
    """Round a Decimal to the nearest integer, halves away from zero."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def ratio(numerator: int, denominator: int) -> Decimal:
    """Exact ratio of two integer minor-unit amounts."""
    if denominator == 0:
        raise ZeroDivisionError("ratio denominator is zero")
    return Decimal(numerator) / Decimal(denominator)


def format_minor(minor: int, places: int = 2) -> str:
    """Render minor units as a fixed-point string: `441700` -> `4417.00`."""
    if places <= 0:
        return str(minor)
    scale = 10**places
    sign = "-" if minor < 0 else ""
    whole, part = divmod(abs(minor), scale)
    return f"{sign}{whole}.{part:0{places}d}"

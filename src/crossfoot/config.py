"""Thresholds shared by the generator and the matcher.

The generator imports these to *construct* and *assert* cases; the matcher imports them to
*decide*. Changing one changes both, which is the point (spec 01 §4).
"""

from decimal import Decimal
from typing import Final


class Thresholds:
    """Single source of truth for every tolerance in the system."""

    # RapidFuzz token_set_ratio, 0-100.
    NAME_SIM_MIN: Final = 80
    # net / gross for a processor-fee deduction. Asymmetric on purpose.
    FEE_WINDOW: Final = (Decimal("0.90"), Decimal("1.02"))
    # Business days around the due date a payment may land in and still be "on time".
    DATE_WINDOW_BD: Final = (-1, 5)
    # Minor units of slack allowed on a sum that is meant to be exact.
    EXACT_SUM_TOL: Final = 0


# Remittance layout the parser is never developed against (spec 01 §6).
HELD_OUT_TEMPLATE: Final = "D"

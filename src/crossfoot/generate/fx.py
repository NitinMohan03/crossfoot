"""Daily GBP-base FX table as an integer random walk (spec 01 §3.5).

Rates are held as parts per million so the walk never touches a float: cross-platform libm
differences in `exp`/`log` would break byte-determinism.
"""

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from random import Random
from typing import Final

from crossfoot.generate.calendars import is_business_day
from crossfoot.money import round_half_up

CURRENCIES: Final = ("USD", "EUR")
START_PPM: Final = {"USD": 1_270_000, "EUR": 1_160_000}
MAX_STEP_PPM: Final = 5_000  # +/- 0.5% per business day

FxTable = Mapping[date, Mapping[str, int]]


def build_fx_table(rng: Random, start: date, end: date) -> dict[date, dict[str, int]]:
    """Rates for every calendar day in `[start, end]`; non-business days carry the last rate."""
    table: dict[date, dict[str, int]] = {}
    current = dict(START_PPM)
    day = start
    while day <= end:
        if is_business_day(day) and day != start:
            for code in CURRENCIES:
                step = rng.randint(-MAX_STEP_PPM, MAX_STEP_PPM)
                current[code] = current[code] * (1_000_000 + step) // 1_000_000
        table[day] = dict(current)
        day += timedelta(days=1)
    return table


def format_rate(rate_ppm: int) -> str:
    """`1270000` -> `1.270000`."""
    whole, part = divmod(rate_ppm, 1_000_000)
    return f"{whole}.{part:06d}"


def rate_decimal(rate_ppm: int) -> Decimal:
    return Decimal(rate_ppm) / Decimal(1_000_000)


def to_base_minor(amount_minor: int, rate_ppm: int) -> int:
    """Convert a foreign amount to GBP pence at `rate_ppm` (foreign units per GBP)."""
    return round_half_up(Decimal(amount_minor) * Decimal(1_000_000) / Decimal(rate_ppm))


def from_base_minor(base_minor: int, rate_ppm: int) -> int:
    """Convert GBP pence to the foreign currency's minor units at `rate_ppm`."""
    return round_half_up(Decimal(base_minor) * Decimal(rate_ppm) / Decimal(1_000_000))


def render_fx_csv(table: FxTable) -> str:
    """`date,USD,EUR` with 6 dp, one row per calendar day, ascending."""
    lines = ["date," + ",".join(CURRENCIES)]
    for day in sorted(table):
        rates = table[day]
        lines.append(day.isoformat() + "," + ",".join(format_rate(rates[c]) for c in CURRENCIES))
    return "\n".join(lines) + "\n"

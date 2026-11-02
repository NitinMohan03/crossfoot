from datetime import date
from decimal import Decimal

from crossfoot.generate.fx import (
    build_fx_table,
    format_rate,
    from_base_minor,
    render_fx_csv,
    to_base_minor,
)
from crossfoot.generate.rng import substream
from crossfoot.money import format_minor, ratio, round_half_up


def test_rates_carry_over_weekends_and_holidays() -> None:
    table = build_fx_table(substream(42, "world:fx"), date(2024, 3, 25), date(2024, 4, 5))
    friday, saturday, sunday = date(2024, 3, 28), date(2024, 3, 30), date(2024, 3, 31)
    assert table[saturday] == table[friday]  # 29 Mar is Good Friday
    assert table[sunday] == table[friday]


def test_walk_is_deterministic_and_bounded() -> None:
    first = build_fx_table(substream(42, "world:fx"), date(2024, 1, 1), date(2024, 2, 1))
    second = build_fx_table(substream(42, "world:fx"), date(2024, 1, 1), date(2024, 2, 1))
    assert first == second
    for day, rates in first.items():
        for code, value in rates.items():
            assert 900_000 < value < 1_600_000, (day, code, value)


def test_rate_rendering_and_conversion_round_trip() -> None:
    assert format_rate(1_270_000) == "1.270000"
    assert format_rate(1_004_500) == "1.004500"
    # 127.00 USD at 1.27 is 100.00 GBP, and back again.
    assert to_base_minor(12_700, 1_270_000) == 10_000
    assert from_base_minor(10_000, 1_270_000) == 12_700


def test_csv_has_one_row_per_calendar_day() -> None:
    table = build_fx_table(substream(1, "fx"), date(2024, 1, 1), date(2024, 1, 10))
    rows = render_fx_csv(table).splitlines()
    assert rows[0] == "date,USD,EUR"
    assert len(rows) == 11
    assert rows[1].startswith("2024-01-01,")


def test_money_helpers_round_half_away_from_zero() -> None:
    assert round_half_up(Decimal("0.5")) == 1
    assert round_half_up(Decimal("1.5")) == 2
    assert round_half_up(Decimal("-0.5")) == -1
    assert format_minor(441_700) == "4417.00"
    assert format_minor(5) == "0.05"
    assert ratio(435_256, 441_700) == Decimal(435_256) / Decimal(441_700)

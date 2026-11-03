"""The FX and holiday tables land in Postgres and are queryable (plan §4.4 verification)."""

from decimal import Decimal
from typing import Any

import psycopg
import pytest

from crossfoot.db.reference import load_reference, rate_ppm
from tests.test_db.conftest import DEV_SLICE

# What the committed dev slice holds: 397 daily rows x 2 currencies, 16 bank holidays.
FX_ROWS = 794
HOLIDAY_ROWS = 16


def scalar(conn: psycopg.Connection[Any], sql: str, *args: Any) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, args or None)
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_rate_ppm_is_exact_or_refuses() -> None:
    assert rate_ppm("1.270000") == 1_270_000
    assert rate_ppm("1.160000") == 1_160_000
    assert rate_ppm("0.999999") == 999_999
    with pytest.raises(ValueError, match="more than 6 decimal places"):
        rate_ppm("1.2700005")


def test_the_load_reports_what_it_wrote(conn: psycopg.Connection[Any]) -> None:
    counts = load_reference(DEV_SLICE, conn)
    assert counts.fx_rates == FX_ROWS
    assert counts.bank_holidays == HOLIDAY_ROWS
    assert scalar(conn, "SELECT count(*) FROM ref.fx_rates") == FX_ROWS
    assert scalar(conn, "SELECT count(*) FROM ref.bank_holidays") == HOLIDAY_ROWS


def test_a_rate_round_trips_through_the_generated_column(conn: psycopg.Connection[Any]) -> None:
    load_reference(DEV_SLICE, conn)
    ppm = scalar(
        conn,
        "SELECT rate_ppm FROM ref.fx_rates WHERE rate_date = %s AND quote_currency = %s",
        "2024-01-01",
        "USD",
    )
    rate = scalar(
        conn,
        "SELECT rate FROM ref.fx_rates WHERE rate_date = %s AND quote_currency = %s",
        "2024-01-01",
        "USD",
    )
    assert ppm == 1_270_000
    # numeric, so exact decimal -- a float column would not compare equal like this.
    assert rate == Decimal("1.270000")
    assert isinstance(rate, Decimal)


def test_both_currencies_are_present_for_every_date(conn: psycopg.Connection[Any]) -> None:
    load_reference(DEV_SLICE, conn)
    assert scalar(conn, "SELECT count(DISTINCT quote_currency) FROM ref.fx_rates") == 2
    assert scalar(conn, "SELECT count(DISTINCT rate_date) FROM ref.fx_rates") == FX_ROWS // 2
    gaps = scalar(
        conn,
        "SELECT count(*) FROM (SELECT rate_date FROM ref.fx_rates "
        "GROUP BY rate_date HAVING count(*) <> 2) AS incomplete",
    )
    assert gaps == 0


def test_the_load_is_repeatable(conn: psycopg.Connection[Any]) -> None:
    """Replace, not upsert: a second run must not duplicate or drift."""
    first = load_reference(DEV_SLICE, conn)
    second = load_reference(DEV_SLICE, conn)
    assert first == second
    assert scalar(conn, "SELECT count(*) FROM ref.fx_rates") == FX_ROWS
    assert scalar(conn, "SELECT count(*) FROM ref.bank_holidays") == HOLIDAY_ROWS


def test_holidays_carry_their_names_and_region(conn: psycopg.Connection[Any]) -> None:
    load_reference(DEV_SLICE, conn)
    name = scalar(conn, "SELECT name FROM ref.bank_holidays WHERE holiday_date = %s", "2024-12-25")
    assert name == "Christmas Day"
    assert scalar(conn, "SELECT count(DISTINCT region) FROM ref.bank_holidays") == 1
    assert scalar(conn, "SELECT DISTINCT region FROM ref.bank_holidays") == "GB-ENG"

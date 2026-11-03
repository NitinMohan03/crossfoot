"""SQL business-day arithmetic must agree with the Python version, exactly.

Two implementations of the same rule is one too many unless they are pinned to each other. The
generator and the matcher use `crossfoot.generate.calendars`; blocking will use the SQL
functions, so a disagreement would silently shift date-tolerance windows. This test sweeps every
date in the dataset's range against every offset the matcher cares about.
"""

from datetime import date
from typing import Any

import psycopg

from crossfoot.config import Thresholds
from crossfoot.db.reference import load_reference
from crossfoot.generate import calendars
from tests.test_db.conftest import DEV_SLICE

START = date(2024, 1, 1)
END = date(2025, 1, 31)
# A little wider than the matcher's own window, so the edges are covered too.
OFFSETS = (Thresholds.DATE_WINDOW_BD[0] - 4, Thresholds.DATE_WINDOW_BD[1] + 5)

SWEEP = """
    SELECT day::date,
           n,
           ref.is_business_day(day::date),
           ref.add_business_days(day::date, n)
      FROM generate_series(%s::date, %s::date, interval '1 day') AS day,
           generate_series(%s::integer, %s::integer) AS n
"""


def test_sql_and_python_agree_on_every_day_in_range(conn: psycopg.Connection[Any]) -> None:
    load_reference(DEV_SLICE, conn)
    lo, hi = OFFSETS
    with conn.cursor() as cur:
        cur.execute(SWEEP, (START, END, lo, hi))
        rows = cur.fetchall()

    expected_rows = (END - START).days + 1
    assert len(rows) == expected_rows * (hi - lo + 1)

    for day, offset, is_business, moved in rows:
        assert is_business == calendars.is_business_day(day), f"is_business_day({day})"
        assert moved == calendars.add_business_days(day, offset), (
            f"add_business_days({day}, {offset})"
        )


def test_the_holiday_table_is_what_the_python_side_reads(conn: psycopg.Connection[Any]) -> None:
    load_reference(DEV_SLICE, conn)
    with conn.cursor() as cur:
        cur.execute("SELECT holiday_date FROM ref.bank_holidays ORDER BY holiday_date")
        loaded = {row[0] for row in cur.fetchall()}
    assert loaded == set(calendars.holidays())


def test_good_friday_is_not_a_business_day(conn: psycopg.Connection[Any]) -> None:
    """A spot check that reads as a fact rather than as a sweep: 2024-03-29 is Good Friday."""
    load_reference(DEV_SLICE, conn)
    with conn.cursor() as cur:
        cur.execute("SELECT ref.is_business_day('2024-03-29'::date)")
        row = cur.fetchone()
        assert row is not None and row[0] is False
        # Thursday 2024-03-28 + 1 business day skips Good Friday and the weekend.
        cur.execute("SELECT ref.add_business_days('2024-03-28'::date, 1)")
        row = cur.fetchone()
        assert row is not None and row[0] == date(2024, 4, 2)

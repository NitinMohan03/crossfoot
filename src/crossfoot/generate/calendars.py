"""Business-day arithmetic against one checked-in holiday list (spec 01 §3.5).

Every business-day calculation in the generator and the matcher goes through this module and
reads `reference/holidays_gb_eng.csv` — no holiday library, no second jurisdiction.
"""

import csv
from datetime import date, timedelta
from functools import lru_cache
from importlib.resources import files

HOLIDAY_FILE = "holidays_gb_eng.csv"


@lru_cache(maxsize=1)
def holidays() -> frozenset[date]:
    """England & Wales bank holidays, 2024-2025."""
    raw = (files("crossfoot.generate.reference") / HOLIDAY_FILE).read_text(encoding="utf-8")
    rows = csv.DictReader(raw.splitlines())
    return frozenset(date.fromisoformat(row["date"]) for row in rows)


@lru_cache(maxsize=1)
def holiday_names() -> dict[date, str]:
    raw = (files("crossfoot.generate.reference") / HOLIDAY_FILE).read_text(encoding="utf-8")
    rows = csv.DictReader(raw.splitlines())
    return {date.fromisoformat(row["date"]): row["name"] for row in rows}


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in holidays()


def next_business_day(day: date) -> date:
    out = day + timedelta(days=1)
    while not is_business_day(out):
        out += timedelta(days=1)
    return out


def previous_business_day(day: date) -> date:
    out = day - timedelta(days=1)
    while not is_business_day(out):
        out -= timedelta(days=1)
    return out


def add_business_days(day: date, count: int) -> date:
    """Move `count` business days from `day`. `count == 0` snaps forward to a business day."""
    if count == 0:
        out = day
        while not is_business_day(out):
            out += timedelta(days=1)
        return out
    step = 1 if count > 0 else -1
    out = day
    for _ in range(abs(count)):
        out += timedelta(days=step)
        while not is_business_day(out):
            out += timedelta(days=step)
    return out


def business_days_between(start: date, end: date) -> int:
    """Signed count of business days from `start` to `end`, counting `end` and not `start`."""
    if end == start:
        return 0
    step = 1 if end > start else -1
    count = 0
    out = start
    while out != end:
        out += timedelta(days=step)
        if is_business_day(out):
            count += step
    return count


def spans_non_business_day(start: date, end: date) -> bool:
    """True when at least one non-business day falls in `(start, end]`."""
    lo, hi = (start, end) if end >= start else (end, start)
    out = lo + timedelta(days=1)
    while out <= hi:
        if not is_business_day(out):
            return True
        out += timedelta(days=1)
    return False


def business_day_on_or_before(day: date) -> date:
    out = day
    while not is_business_day(out):
        out -= timedelta(days=1)
    return out


def business_day_on_or_after(day: date) -> date:
    out = day
    while not is_business_day(out):
        out += timedelta(days=1)
    return out

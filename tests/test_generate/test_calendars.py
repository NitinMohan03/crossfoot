from datetime import date

from crossfoot.generate.calendars import (
    add_business_days,
    business_day_on_or_before,
    business_days_between,
    holidays,
    is_business_day,
    next_business_day,
    spans_non_business_day,
)


def test_holiday_list_covers_both_years() -> None:
    days = holidays()
    assert date(2024, 12, 26) in days
    assert date(2025, 4, 18) in days
    assert len([day for day in days if day.year == 2024]) == 8


def test_easter_weekend_is_not_a_business_week() -> None:
    assert not is_business_day(date(2024, 3, 29))  # Good Friday
    assert not is_business_day(date(2024, 4, 1))  # Easter Monday
    assert next_business_day(date(2024, 3, 28)) == date(2024, 4, 2)


def test_add_business_days_skips_christmas() -> None:
    # 24 Dec 2024 is a Tuesday; 25th and 26th are holidays, then a weekend.
    assert add_business_days(date(2024, 12, 24), 1) == date(2024, 12, 27)
    assert add_business_days(date(2024, 12, 24), 2) == date(2024, 12, 30)


def test_add_business_days_zero_snaps_forward_and_negative_steps_back() -> None:
    assert add_business_days(date(2024, 6, 1), 0) == date(2024, 6, 3)  # Saturday -> Monday
    assert add_business_days(date(2024, 6, 3), -1) == date(2024, 5, 31)


def test_business_days_between_is_signed_and_excludes_the_start() -> None:
    assert business_days_between(date(2024, 6, 3), date(2024, 6, 7)) == 4
    assert business_days_between(date(2024, 6, 7), date(2024, 6, 3)) == -4
    assert business_days_between(date(2024, 6, 3), date(2024, 6, 3)) == 0


def test_span_detection_and_on_or_before() -> None:
    assert spans_non_business_day(date(2024, 6, 7), date(2024, 6, 10))  # crosses a weekend
    assert not spans_non_business_day(date(2024, 6, 3), date(2024, 6, 5))
    assert business_day_on_or_before(date(2024, 6, 2)) == date(2024, 5, 31)
    assert business_day_on_or_before(date(2024, 6, 3)) == date(2024, 6, 3)

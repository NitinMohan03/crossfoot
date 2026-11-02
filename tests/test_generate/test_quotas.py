from crossfoot.generate.quotas import IMPLEMENTED, NATURAL_RATES, allocate


def test_natural_rates_sum_to_one_hundred() -> None:
    assert sum(NATURAL_RATES.values()) == 100


def test_session_1_2_allocation_renormalises_three_types() -> None:
    counts = allocate(10_000, IMPLEMENTED)
    assert counts == {"clean": 7_600, "fee": 1_333, "timing": 1_067}
    assert sum(counts.values()) == 10_000


def test_allocation_is_exact_for_awkward_totals() -> None:
    for total in (1, 7, 250, 999, 1_200):
        counts = allocate(total, IMPLEMENTED)
        assert sum(counts.values()) == total


def test_full_rate_table_allocates_the_plan_counts() -> None:
    counts = allocate(10_000, tuple(NATURAL_RATES))
    assert counts["clean"] == 5_700
    assert counts["fee"] == 1_000
    assert counts["no_match"] == 100

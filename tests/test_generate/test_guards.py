"""The test slice and the golden tree are opt-in only (spec 01 §8.2, §8.3)."""

from pathlib import Path

import pytest

from crossfoot.generate.guards import GuardError, require_golden_opt_in, require_test_opt_in
from crossfoot.generate.seeds import DEV_SEED, FULL_SEED, TEST_SEED


def test_test_seed_refuses_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CROSSFOOT_ALLOW_TEST", raising=False)
    with pytest.raises(GuardError, match="held out until Phase 8"):
        require_test_opt_in(TEST_SEED)
    require_test_opt_in(FULL_SEED)
    require_test_opt_in(DEV_SEED)


def test_test_seed_runs_with_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSFOOT_ALLOW_TEST", "1")
    require_test_opt_in(TEST_SEED)


def test_golden_writes_refuse_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CROSSFOOT_REGEN_GOLDEN", raising=False)
    golden = Path("tests/fixtures/golden/dev")
    with pytest.raises(GuardError, match="ground truth"):
        require_golden_opt_in(golden)
    require_golden_opt_in(Path("data/full"))

    monkeypatch.setenv("CROSSFOOT_REGEN_GOLDEN", "1")
    require_golden_opt_in(golden)


def test_seeds_are_distinct() -> None:
    assert len({FULL_SEED, DEV_SEED, TEST_SEED}) == 3

"""Opt-in gates around the test slice and the golden directory (spec 01 §8.2, §8.3)."""

import os
from pathlib import Path

from crossfoot.generate.seeds import TEST_SEED

GOLDEN = Path("tests") / "fixtures" / "golden"


class GuardError(RuntimeError):
    """A deliberate refusal: the caller must set an environment flag and mean it."""


def require_test_opt_in(seed: int) -> None:
    if seed == TEST_SEED and os.environ.get("CROSSFOOT_ALLOW_TEST") != "1":
        raise GuardError(
            "refusing to generate from TEST_SEED: the test slice is held out until Phase 8. "
            "Set CROSSFOOT_ALLOW_TEST=1 if you really mean it (spec 01 §8.2)."
        )


def require_golden_opt_in(out_dir: Path) -> None:
    parts = out_dir.resolve().parts
    needle = GOLDEN.parts
    for start in range(len(parts) - len(needle) + 1):
        if parts[start : start + len(needle)] == needle:
            if os.environ.get("CROSSFOOT_REGEN_GOLDEN") != "1":
                raise GuardError(
                    f"refusing to write {out_dir}: tests/fixtures/golden/ is ground truth. "
                    "Use `just golden` (sets CROSSFOOT_REGEN_GOLDEN=1) for a reviewed "
                    "regeneration (spec 01 §8.3)."
                )
            return

"""Dataset seeds.

`TEST_SEED` is read by nothing in this repo except the slice builder's guard. The test slice is
not generated until Phase 8 (spec 01 §8.2).
"""

from typing import Final

FULL_SEED: Final = 42
DEV_SEED: Final = 20240914
TEST_SEED: Final = 86753099

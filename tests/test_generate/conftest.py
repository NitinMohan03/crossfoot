"""A small generated dataset, built once per test session."""

from pathlib import Path

import pytest

from crossfoot.generate.dataset import generate, write

SEED = 7
EVENTS = 250


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dataset") / "full"
    write(out, generate(SEED, "full", EVENTS))
    return out

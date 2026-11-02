"""The determinism contract (spec 01 §9)."""

from pathlib import Path

from crossfoot.generate.dataset import generate, write
from crossfoot.generate.hashes import compare, tree_hashes
from crossfoot.generate.rng import substream
from tests.test_generate.conftest import EVENTS, SEED


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    left, right = tmp_path / "a", tmp_path / "b"
    write(left, generate(SEED, "full", EVENTS))
    write(right, generate(SEED, "full", EVENTS))
    assert compare(left, right) == []
    # 13 bank CSVs + posted_at, settlements, batches, erp.sql, fx, holidays, parents, manifest
    assert len(tree_hashes(left)) == 21


def test_a_different_seed_changes_the_data(tmp_path: Path) -> None:
    left, right = tmp_path / "a", tmp_path / "b"
    write(left, generate(SEED, "full", EVENTS))
    write(right, generate(SEED + 1, "full", EVENTS))
    assert compare(left, right) != []


def test_substreams_depend_only_on_seed_and_label() -> None:
    assert substream(42, "event:clean:7").random() == substream(42, "event:clean:7").random()
    assert substream(42, "event:clean:7").random() != substream(42, "event:clean:8").random()
    assert substream(42, "event:clean:7").random() != substream(43, "event:clean:7").random()

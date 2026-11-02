"""The determinism contract (spec 01 §9)."""

import json
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


def test_the_written_file_set_is_exactly_what_the_spec_lists(tmp_path: Path) -> None:
    """Every file spec 01 §3 names, and nothing else.

    The count is not hard-coded any more: the remittance advices are one PDF per advice, so it
    moves with the quotas. The invariant is the *set* of non-PDF files plus "one PDF per line of
    remit.jsonl" (§3.4).
    """
    out = tmp_path / "set"
    write(out, generate(SEED, "full", EVENTS))
    names = set(tree_hashes(out))

    expected = {
        "erp.sql",
        "fx_rates.csv",
        "holidays_gb_eng.csv",
        "manifest.jsonl",
        "parent_registry.jsonl",
        "bank/posted_at.jsonl",
        "processor/settlements.jsonl",
        "processor/batches.jsonl",
        "remit/remit.jsonl",
    }
    # 13 monthly bank CSVs, 2024-01 .. 2025-01 (§3.2).
    csvs = {name for name in names if name.startswith("bank/") and name.endswith(".csv")}
    assert len(csvs) == 13
    pdfs = {name for name in names if name.endswith(".pdf")}
    assert all(name.startswith("remit/") for name in pdfs)
    assert names - csvs - pdfs == expected

    advices = (out / "remit" / "remit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(pdfs) == len(advices)
    assert pdfs == {f"remit/{json.loads(line)['payment_ref']}.pdf" for line in advices}


def test_a_different_seed_changes_the_data(tmp_path: Path) -> None:
    left, right = tmp_path / "a", tmp_path / "b"
    write(left, generate(SEED, "full", EVENTS))
    write(right, generate(SEED + 1, "full", EVENTS))
    assert compare(left, right) != []


def test_substreams_depend_only_on_seed_and_label() -> None:
    assert substream(42, "event:clean:7").random() == substream(42, "event:clean:7").random()
    assert substream(42, "event:clean:7").random() != substream(42, "event:clean:8").random()
    assert substream(42, "event:clean:7").random() != substream(43, "event:clean:7").random()

"""The validator must pass a good dataset and catch a broken one."""

import json
from pathlib import Path

from crossfoot.generate import validate


def test_every_implemented_check_passes(dataset_dir: Path) -> None:
    results = validate.run(dataset_dir)
    failures = {result.name: result.failures for result in results if not result.ok}
    assert failures == {}
    assert len(results) == 7


def test_a_tampered_settlement_is_caught(dataset_dir: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    for source in dataset_dir.rglob("*"):
        if source.is_file():
            target = broken / source.relative_to(dataset_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

    path = broken / "processor" / "settlements.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["net_minor"] += 1
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )

    results = {result.name: result for result in validate.run(broken)}
    assert not results["3. arithmetic"].ok


def test_a_missing_manifest_group_is_caught(dataset_dir: Path, tmp_path: Path) -> None:
    broken = tmp_path / "missing"
    broken.mkdir()
    for source in dataset_dir.rglob("*"):
        if source.is_file():
            target = broken / source.relative_to(dataset_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

    path = broken / "manifest.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")

    results = {result.name: result for result in validate.run(broken)}
    assert not results["1. counts"].ok
    assert not results["2. referential integrity"].ok

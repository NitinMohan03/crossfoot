"""Per-type regression pins (spec 01 §9).

These hashes cover the manifest groups of one type at a time, so a change aimed at one
discrepancy type shows up immediately if it moves another type's records. They are pinned to
the current quota configuration -- Session 1.3 renormalises the quotas and will repin them.
"""

import hashlib
import json
from pathlib import Path

PINS = {
    "batch_payout": "014eb0875af1c23349b1f942a986e9142816c659e03f2275f7e01bf3afee790a",
    "clean": "b1e8bd634547a47887d55cf39048834faf34f096d2aba77cb1bca6812e3ba8f2",
    "fee": "8919f9e09f9dbca9feddb31fef7d69369dc2de87154a6650d78fb031b5e667ea",
    "timing": "ad0d6d31d290b6c60ae6c75821a6d08d337fdbc19c6ef0d95e692902b234131a",
}

COUNTS = {"batch_payout": 98, "clean": 190, "fee": 33, "timing": 27}


def per_type_hashes(manifest: Path) -> dict[str, str]:
    buckets: dict[str, list[str]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines()[1:]:
        group = json.loads(line)
        buckets.setdefault(str(group.get("kind") or group["match_type"]), []).append(line)
    return {
        name: hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()
        for name, lines in buckets.items()
    }


def test_each_type_hashes_to_its_pin(dataset_dir: Path) -> None:
    assert per_type_hashes(dataset_dir / "manifest.jsonl") == PINS


def test_group_counts_are_stable(dataset_dir: Path) -> None:
    lines = (dataset_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[1:]
    observed: dict[str, int] = {}
    for line in lines:
        group = json.loads(line)
        key = str(group.get("kind") or group["match_type"])
        observed[key] = observed.get(key, 0) + 1
    assert observed == COUNTS

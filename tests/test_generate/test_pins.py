"""Per-type regression pins (spec 01 §9).

Each hash covers the manifest groups of one class at a time, so a change aimed at one
discrepancy type shows up immediately if it moves another type's records. That is the §9
contract: "changing the generator for one type must not change any other type's records for the
same seed."

Two things legitimately move every pin at once, and nothing else should:

  * the quota configuration, because invoice refs are globally sequential in issue-date order,
    so adding or resizing a class renumbers everybody (documented in `events.py`); and
  * the world (counterparty pool, FX walk), for the same reason.

Session 1.3 repinned these when the natural rates were restored.
"""

import hashlib
import json
from pathlib import Path

PINS = {
    "batch_payout": "bf1e1ea081e409a51cb76d744261ec754c9b0b43061f1b26f8f300b3739b9876",
    "clean": "815cc49c5569202af02ae7ac7dfd9fafce5450e481e7d2969b9716e2b06488e9",
    "consolidated": "4762f02d978ac0aacaafddd8bcb8194f7d1ea3976b215bd21daf15e00252eaf7",
    "duplicate": "d28b4751031bd5c4c1de0b3b94f3a32b4c7f4af16e684d41512d18f6a01fa143",
    "duplicate:copy": "7b30b43556e683a1c59dc5ab3c7072fa1ad7c50c9cea5a1171b030fdeeaa4edd",
    "fee": "5f2c83073d55799ccd4ccf5bcf3e295c82f4ffc9d06266efc0d49a46253ddb3f",
    "fx": "d0d9e2ac1952bf396f54a2a1ca7e09843d8be8bd1042ab6809455ecbad885859",
    "name": "36483e23b5676c7f163f002f8faa7cbe4d696ecb2e0b16212610b4ca4473e1b1",
    "open_invoice": "fd0c8957eb72043f8c77694d162d7c1cd90a37cc269ff523e3d514432ef53ae4",
    "partial": "0ae66bbd4f38da77ea96cf70fb10f651732a0d823d0b14d5678c500a8ebc7b47",
    "reasoning": "3c5500195e82225298d4db17d59c6860c67c5a312a033e50f393a7fdabf6cbff",
    "refund": "224f50a329003c5be8d1f7fac439e2fa61affbe998eb997c1d3cd438b2c739a2",
    "timing": "4659b786fc56a8668c687a089680bd97cfbe4feee22f2a1356379bad43cb8460",
    "unknown_receipt": "08f3aef595d52c0b73ae8fdf67a20867120e3f8932e7a58eb5896d1a9cc9dbee",
}

COUNTS = {
    "batch_payout": 99,
    "clean": 143,
    "consolidated": 10,
    "duplicate": 5,
    "duplicate:copy": 5,
    "fee": 25,
    "fx": 7,
    "name": 18,
    "open_invoice": 1,
    "partial": 13,
    "reasoning": 7,
    "refund": 10,
    "timing": 20,
    "unknown_receipt": 1,
}


def bucket_of(group: dict[str, object]) -> str:
    """The pin bucket a manifest group belongs to.

    A `no_match` group is keyed by its `kind`, so the two event orphans and the two noise kinds
    pin separately. A duplicate *copy* is keyed apart from the event it copies: they share a
    match type but are produced by different code, so conflating them would blur the pin.
    """
    if group.get("kind"):
        return str(group["kind"])
    match_type = str(group["match_type"])
    if match_type == "duplicate" and not group.get("event_id"):
        return "duplicate:copy"
    return match_type


def per_type_groups(manifest: Path) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines()[1:]:
        buckets.setdefault(bucket_of(json.loads(line)), []).append(line)
    return buckets


def per_type_hashes(manifest: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()
        for name, lines in per_type_groups(manifest).items()
    }


def test_each_type_hashes_to_its_pin(dataset_dir: Path) -> None:
    assert per_type_hashes(dataset_dir / "manifest.jsonl") == PINS


def test_group_counts_are_stable(dataset_dir: Path) -> None:
    observed = {
        name: len(lines) for name, lines in per_type_groups(dataset_dir / "manifest.jsonl").items()
    }
    assert observed == COUNTS

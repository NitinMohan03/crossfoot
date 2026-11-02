"""A manifest group and a MatchDecision must reduce to the same key (spec 01 §5.1)."""

from decimal import Decimal
from pathlib import Path

import pytest

from crossfoot.eval import group_key
from crossfoot.generate.read import read_manifest
from crossfoot.models import Evidence, MatchDecision


def test_manifest_group_and_decision_round_trip(dataset_dir: Path) -> None:
    _, groups = read_manifest(dataset_dir / "manifest.jsonl")
    group = next(item for item in groups if item["match_type"] == "fee")

    decision = MatchDecision(
        source_ids=list(reversed(group["source_ids"])),
        target_ids=list(group["target_ids"]),
        match_type="fee",
        action="match",
        evidence=[Evidence(record_id=group["target_ids"][0], field="fee_minor", value="6444")],
        score=Decimal("0.97"),
    )
    assert group_key(decision) == group_key(group)


def test_evidence_and_order_do_not_affect_identity() -> None:
    group = {
        "source_ids": ["erp:INV-2024-0002", "erp:INV-2024-0001"],
        "target_ids": ["bank:2024-01-16:000004"],
        "evidence_ids": ["remit:RA-2024-0088"],
    }
    decision = MatchDecision(
        source_ids=["erp:INV-2024-0001", "erp:INV-2024-0002"],
        target_ids=["bank:2024-01-16:000004"],
        match_type="consolidated",
        action="match",
    )
    assert group_key(group) == group_key(decision)


def test_partial_overlap_is_a_different_group() -> None:
    truth = {"source_ids": ["erp:A", "erp:B"], "target_ids": ["bank:1"]}
    predicted = MatchDecision(
        source_ids=["erp:A"], target_ids=["bank:1"], match_type="clean", action="match"
    )
    assert group_key(truth) != group_key(predicted)


def test_empty_sides_are_keyed_not_dropped() -> None:
    orphan = {"source_ids": ["erp:A"], "target_ids": []}
    receipt = {"source_ids": [], "target_ids": ["bank:1"]}
    assert group_key(orphan) == (frozenset({"erp:A"}), frozenset())
    assert group_key(receipt) == (frozenset(), frozenset({"bank:1"}))


def test_a_bare_string_is_rejected() -> None:
    with pytest.raises(TypeError):
        group_key({"source_ids": "erp:A", "target_ids": []})

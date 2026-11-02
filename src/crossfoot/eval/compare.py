"""Group identity: the one function that decides whether two groups are the same group.

A manifest group and a `MatchDecision` must reduce to the same key, or the eval is comparing
apples to pears (spec 01 §5.1).
"""

from collections.abc import Mapping
from typing import Any

from crossfoot.models.decision import MatchDecision

GroupKey = tuple[frozenset[str], frozenset[str]]


def _ids(value: Any, field: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError(f"{field} must be a sequence of record ids, got {type(value).__name__}")
    ids = list(value)
    if not all(isinstance(item, str) for item in ids):
        raise TypeError(f"{field} must contain only record ids (str)")
    return frozenset(ids)


def group_key(obj: MatchDecision | Mapping[str, Any]) -> GroupKey:
    """Scoring identity of a group: `(frozenset(source_ids), frozenset(target_ids))`.

    Accepts a `MatchDecision` or a parsed `manifest.jsonl` group. `match_type` is compared
    separately as a secondary metric; `evidence_ids` never affect identity.
    """
    if isinstance(obj, MatchDecision):
        return frozenset(obj.source_ids), frozenset(obj.target_ids)
    return _ids(obj.get("source_ids"), "source_ids"), _ids(obj.get("target_ids"), "target_ids")

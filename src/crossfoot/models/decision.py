"""The system's output record (spec 01 §5.4).

`MatchDecision` is what the pipeline emits and what the eval compares against a manifest group.
It is deliberately dumb: the model never computes the score, Python does.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

MatchType = Literal[
    "clean",
    "fee",
    "timing",
    "name",
    "partial",
    "consolidated",
    "fx",
    "reasoning",
    "duplicate",
    "no_match",
]

Action = Literal["match", "flag", "escalate"]

MATCH_TYPES: tuple[MatchType, ...] = (
    "clean",
    "fee",
    "timing",
    "name",
    "partial",
    "consolidated",
    "fx",
    "reasoning",
    "duplicate",
    "no_match",
)


@dataclass(slots=True)
class Evidence:
    """One cited field from one record. The LLM must point at something real."""

    record_id: str
    field: str
    value: str


@dataclass(slots=True)
class MatchDecision:
    source_ids: list[str]
    target_ids: list[str]
    match_type: MatchType
    action: Action
    evidence: list[Evidence] = field(default_factory=list)
    score: Decimal = Decimal("0")

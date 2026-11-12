"""Entity resolution: counterparty names to counterparty ids.

Deliberately not under `matching/`: this is the normalisation layer the matcher reads from, not
a matching rule. Three pieces -- the candidate `NameIndex`, the human-confirmed `AliasRegistry`,
and the `resolve` ladder over both (plan §2.7).
"""

from crossfoot.entities.index import Candidate, NameIndex
from crossfoot.entities.registry import (
    EVAL_ENV,
    PROVENANCE,
    Alias,
    AliasRegistry,
    RegistryFrozen,
    eval_run,
)
from crossfoot.entities.resolver import resolve, resolve_description

__all__ = [
    "EVAL_ENV",
    "PROVENANCE",
    "Alias",
    "AliasRegistry",
    "Candidate",
    "NameIndex",
    "RegistryFrozen",
    "eval_run",
    "resolve",
    "resolve_description",
]

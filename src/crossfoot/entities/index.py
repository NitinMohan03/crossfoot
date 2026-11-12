"""The candidate names entity resolution resolves against.

Built from rows and holding no connection of its own, so the resolver is testable without
Postgres and the same index can be filled from any source's counterparty list.

Ambiguity is tracked rather than resolved by luck: if two counterparties normalise to the same
string, neither is a deterministic answer for that string, and the index says so. Picking the
first one would make results depend on row order, which is the failure mode plan §2.7 warns
about for the registry and is no better here.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from crossfoot.names import normalise


@dataclass(frozen=True, slots=True)
class Candidate:
    """One counterparty as the ERP knows it. `trading_name` may equal `legal_name`."""

    counterparty_id: int
    legal_name: str
    trading_name: str

    @property
    def names(self) -> tuple[str, ...]:
        """Legal and trading name, de-duplicated, in a stable order."""
        if self.trading_name == self.legal_name:
            return (self.legal_name,)
        return (self.legal_name, self.trading_name)


class NameIndex:
    """Normalised-name lookup plus the candidate list the fuzzy layer scans."""

    def __init__(self, candidates: Iterable[Candidate]) -> None:
        self._candidates: Final = tuple(candidates)
        exact: dict[str, int] = {}
        ambiguous: set[str] = set()
        for candidate in self._candidates:
            for name in candidate.names:
                key = normalise(name)
                if not key:
                    continue
                seen = exact.get(key)
                if seen is not None and seen != candidate.counterparty_id:
                    ambiguous.add(key)
                exact.setdefault(key, candidate.counterparty_id)
        self._exact: Final[Mapping[str, int]] = exact
        self._ambiguous: Final[frozenset[str]] = frozenset(ambiguous)

    @property
    def candidates(self) -> Sequence[Candidate]:
        return self._candidates

    @property
    def ambiguous(self) -> frozenset[str]:
        """Normalised names two or more counterparties share."""
        return self._ambiguous

    def __len__(self) -> int:
        return len(self._candidates)

    def lookup_normalised(self, name: str) -> int | None:
        """The counterparty whose legal or trading name normalises to `name`, if exactly one."""
        key = normalise(name)
        if not key or key in self._ambiguous:
            return None
        return self._exact.get(key)

    def names_for(self, counterparty_id: int) -> tuple[str, ...]:
        for candidate in self._candidates:
            if candidate.counterparty_id == counterparty_id:
                return candidate.names
        return ()

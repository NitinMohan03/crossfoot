"""Entity resolution: a payer's surface name to a counterparty id.

Four layers, first hit wins, each one cheaper and more certain than the next:

1. **exact** -- the surface is a counterparty's name, character for character.
2. **normalised** -- it is that name modulo case, punctuation, legal suffix and the bank's
   32-character truncation (`crossfoot.names`, shared with the generator).
3. **alias** -- a human confirmed it (`crossfoot.entities.registry`). This is the layer that
   catches DBA and parent-company names, which no string metric relates.
4. **fuzzy** -- RapidFuzz `token_set_ratio` at or above `Thresholds.NAME_SIM_MIN`. The
   threshold is imported; the number appears nowhere in this package.

**No layer may answer by row order.** Every one of them collects all the counterparties it
matches and answers only if there is exactly one: two counterparties matching equally well is
not an answer, whether the tie is a shared normalised name, two names that both truncate to the
surface, or two equal fuzzy scores. Taking the first would make the result depend on the order
rows came back in, which is the failure plan §2.7 calls out for the registry and which is no
better here.

`resolve_description` is the same ladder over a free-text field that may carry a payer-typed
reference after the name (`ACME WIDGETS LTD INV-2024-0891`). Nothing marks the boundary, so the
candidate forms are every leading run of whole tokens -- the recovery
`crossfoot.names.token_prefixes` exists for, done here once rather than guessed again in each
source's server.
"""

from collections.abc import Sequence

from crossfoot.config import Thresholds
from crossfoot.entities.index import NameIndex
from crossfoot.entities.registry import AliasRegistry
from crossfoot.models.canonical import PartyRef, ResolutionMethod
from crossfoot.names import (
    best_similarity,
    normalise,
    surface_similarity,
    token_prefixes,
    trivially_normalises,
)

#: What `similarity` reads when a layer identified the party structurally rather than by score.
STRUCTURAL: int = 100


def _party(
    surface: str,
    counterparty_id: int | None,
    method: ResolutionMethod,
    similarity: int,
) -> PartyRef:
    return PartyRef(
        surface=surface,
        normalised=normalise(surface),
        counterparty_id=counterparty_id,
        method=method,
        similarity=similarity,
    )


def _unresolved(surface: str, similarity: int = 0) -> PartyRef:
    """Unresolved, carrying the best score seen so the near miss can be inspected later."""
    return _party(surface, None, "unresolved", similarity)


def _only(ids: set[int]) -> int | None:
    """The single id in `ids`, or None when the layer found none or more than one."""
    return next(iter(ids)) if len(ids) == 1 else None


def _exact_ids(index: NameIndex, forms: Sequence[str]) -> set[int]:
    return {
        candidate.counterparty_id
        for candidate in index.candidates
        if any(form in candidate.names for form in forms)
    }


def _normalised_ids(index: NameIndex, forms: Sequence[str]) -> set[int]:
    """Counterparties whose name normalises to a form, truncation included (spec 01 §4.1)."""
    found: set[int] = set()
    for candidate in index.candidates:
        known = {normalise(name) for name in candidate.names}
        for form in forms:
            key = normalise(form)
            truncated = any(trivially_normalises(name, form) for name in candidate.names)
            if (key and key in known) or truncated:
                found.add(candidate.counterparty_id)
                break
    return found


def _alias_ids(registry: AliasRegistry, forms: Sequence[str]) -> set[int]:
    return {found for form in forms if (found := registry.lookup(form)) is not None}


def _best_fuzzy(index: NameIndex, surface: str, *, prefixes: bool) -> tuple[int | None, int]:
    """The single best-scoring candidate and its score, or `(None, best)` on a tie."""
    scores = [
        (
            surface_similarity(candidate.names, surface)
            if prefixes
            else best_similarity(candidate.names, surface),
            candidate.counterparty_id,
        )
        for candidate in index.candidates
    ]
    if not scores:
        return None, 0
    best = max(score for score, _ in scores)
    if best < Thresholds.NAME_SIM_MIN:
        return None, best
    return _only({counterparty_id for score, counterparty_id in scores if score == best}), best


def _resolve(
    surface: str,
    forms: Sequence[str],
    index: NameIndex,
    registry: AliasRegistry,
    *,
    prefixes: bool,
) -> PartyRef:
    if not normalise(surface):
        return _unresolved(surface)

    for method, ids in (
        ("exact", _exact_ids(index, forms)),
        ("normalised", _normalised_ids(index, forms)),
        ("alias", _alias_ids(registry, forms)),
    ):
        if not ids:
            continue
        found = _only(ids)
        if found is None:
            # Two counterparties identified equally well. Nothing further can improve on that,
            # and picking one would be picking by row order.
            return _unresolved(surface, STRUCTURAL)
        return _party(surface, found, method, STRUCTURAL)  # type: ignore[arg-type]

    winner, best = _best_fuzzy(index, surface, prefixes=prefixes)
    if winner is None:
        return _unresolved(surface, best)
    return _party(surface, winner, "fuzzy", best)


def resolve(surface: str, index: NameIndex, registry: AliasRegistry) -> PartyRef:
    """Resolve a payer name that is only a name (an ERP vendor query, an advice header)."""
    return _resolve(surface, (surface,), index, registry, prefixes=False)


def resolve_description(description: str, index: NameIndex, registry: AliasRegistry) -> PartyRef:
    """Resolve a free-text field whose leading tokens are a name and the rest may be anything.

    The bank's `Description` is one 32-character field holding a payer name and, about half the
    time, a reference the payer typed. `surface` on the result stays the whole field: it is what
    the source wrote, and the matcher may still want the part that was not the name.
    """
    return _resolve(description, token_prefixes(description), index, registry, prefixes=True)

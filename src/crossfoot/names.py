"""Counterparty-name normalisation, shared by the generator and the matcher.

The generator uses it to assert that `clean` payer names survive trivial normalisation; the
matcher uses it before any fuzzy comparison.
"""

import re
from collections.abc import Iterable
from typing import Final

from rapidfuzz import fuzz

LEGAL_SUFFIXES: Final = (
    "LIMITED",
    "LTD",
    "PLC",
    "LLP",
    "LLC",
    "INC",
    "AND CO",
    "CO",
)

_PUNCT: Final = re.compile(r"[^A-Z0-9 ]+")
_SPACE: Final = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Upper-case, drop punctuation and legal suffixes, collapse whitespace."""
    out = name.upper().replace("&", " AND ")
    out = _PUNCT.sub(" ", out)
    out = _SPACE.sub(" ", out).strip()
    tokens = out.split(" ")
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    if len(tokens) > 1 and tokens[-1] == "AND":
        tokens.pop()
    return " ".join(tokens)


def similarity(left: str, right: str) -> int:
    """RapidFuzz `token_set_ratio` over normalised names, 0-100 (spec 01 §4 `NAME_SIM_MIN`).

    Rounded to an integer: the generator asserts against this and the matcher decides on it, so
    neither should be comparing a binary float to a threshold.
    """
    score: object = fuzz.token_set_ratio(normalise(left), normalise(right))
    if not isinstance(score, (int, float)):
        raise TypeError(f"token_set_ratio returned {type(score).__name__}")
    return round(score)


def sort_similarity(left: str, right: str) -> int:
    """RapidFuzz `token_sort_ratio` over normalised names, 0-100.

    The companion to `similarity`, and not a second threshold. `token_set_ratio` returns 100
    whenever one name's tokens are a *subset* of the other's, so a counterparty trading as
    `STANWICK` scores 100 against `STANWICK UTILITI3S` -- higher than the `STANWICK UTILITIES`
    the payer actually meant, which a typo drops to 94. `token_sort_ratio` cannot be inflated
    that way, so entity resolution uses it to *rank* the candidates the shared threshold let
    through. The threshold itself stays `token_set_ratio`, which is what spec 01 §4 defines and
    what the generator asserts against.
    """
    score: object = fuzz.token_sort_ratio(normalise(left), normalise(right))
    if not isinstance(score, (int, float)):
        raise TypeError(f"token_sort_ratio returned {type(score).__name__}")
    return round(score)


def ordered_similarity(left: str, right: str) -> int:
    """RapidFuzz plain `ratio` over normalised names, 0-100: order kept, nothing sorted.

    The second companion to `similarity`, for a field that is read left to right. Neither token
    metric is safe on its own there: `token_set_ratio` is 100 for any subset, so one shared
    leading word passes, and `token_sort_ratio` re-orders the tokens first, so a typo in a
    name's first letter can move that token past its neighbour and make `PAZRICK FABRICATION`
    look nearer `GARRICK FABRICATION` than the equally distant `CARRICK FABRICATION`. A bank
    description does not re-order a payer's name, so the plain ratio is the honest measure of
    how much of it survived.
    """
    score: object = fuzz.ratio(normalise(left), normalise(right))
    if not isinstance(score, (int, float)):
        raise TypeError(f"ratio returned {type(score).__name__}")
    return round(score)


def best_similarity(candidates: Iterable[str], surface: str) -> int:
    """The highest similarity of `surface` against any of `candidates` (legal, trading, ...)."""
    return max((similarity(candidate, surface) for candidate in candidates), default=0)


def best_sort_similarity(candidates: Iterable[str], surface: str) -> int:
    """The highest `sort_similarity` of `surface` against any of `candidates`."""
    return max((sort_similarity(candidate, surface) for candidate in candidates), default=0)


def token_prefixes(text: str) -> list[str]:
    """Every leading run of whole tokens in `text`, longest first.

    A bank description is one field holding a payer name and possibly a reference the payer
    typed (`ACME WIDGETS LTD INV-2024-0891`). Nothing marks the boundary, so both the generator
    and the matcher recover the name by trying each prefix -- which is why this lives here,
    shared, rather than being guessed twice.
    """
    tokens = text.split(" ")
    return [" ".join(tokens[:size]) for size in range(len(tokens), 0, -1)]


def surface_similarity(candidates: Iterable[str], description: str) -> int:
    """Best similarity of any token prefix of `description` against any of `candidates`."""
    names = list(candidates)
    return max(
        (best_similarity(names, prefix) for prefix in token_prefixes(description)), default=0
    )


def surface_sort_similarity(candidates: Iterable[str], description: str) -> int:
    """Best `sort_similarity` of any token prefix of `description` against any of `candidates`."""
    names = list(candidates)
    return max(
        (best_sort_similarity(names, prefix) for prefix in token_prefixes(description)), default=0
    )


def normalises_to_any(candidates: Iterable[str], description: str) -> bool:
    """True when some token prefix of `description` trivially normalises to some candidate."""
    names = list(candidates)
    return any(
        trivially_normalises(name, prefix)
        for prefix in token_prefixes(description)
        for name in names
    )


def _drop_partial_suffix(tokens: list[str]) -> list[str]:
    """Drop a trailing token that is only the start of a legal suffix: `... LTD` -> `... LT`."""
    if len(tokens) > 1:
        tail = tokens[-1]
        if any(suffix.startswith(tail) and suffix != tail for suffix in LEGAL_SUFFIXES):
            return tokens[:-1]
    return tokens


def trivially_normalises(legal: str, surface: str) -> bool:
    """True when `surface` is `legal` modulo case, punctuation, suffixes and truncation.

    Truncation is accepted only while the distinguishing tokens survive and enough of the name
    remains to be recognisable -- the boundary spec 01 §4.1 draws between `ACME CORPORATION LT`
    (clean) and `ACME` (a name variant).
    """
    legal_tokens = normalise(legal).split(" ") if normalise(legal) else []
    surface_norm = normalise(surface)
    if not surface_norm or not legal_tokens:
        return False
    surface_tokens = _drop_partial_suffix(surface_norm.split(" "))
    if not surface_tokens or len(surface_tokens) > len(legal_tokens):
        return False
    for position, token in enumerate(surface_tokens[:-1]):
        if token != legal_tokens[position]:
            return False
    last = surface_tokens[-1]
    expected = legal_tokens[len(surface_tokens) - 1]
    if last != expected and not expected.startswith(last):
        return False
    if len(surface_tokens) == len(legal_tokens) and last == expected:
        return True
    return len(" ".join(surface_tokens)) >= 12

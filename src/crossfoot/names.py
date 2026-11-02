"""Counterparty-name normalisation, shared by the generator and the matcher.

The generator uses it to assert that `clean` payer names survive trivial normalisation; the
matcher uses it before any fuzzy comparison.
"""

import re
from typing import Final

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

"""Deterministic random substreams (spec 01 §9).

The master stream allocates quotas and ordering. Every other draw comes from a substream keyed
by a stable label, so adding a discrepancy type cannot perturb the draws of another. Python's
built-in `hash()` is never used: string hashing is salted per process.
"""

from collections.abc import Sequence
from hashlib import blake2b
from random import Random


def substream(seed: int, label: str) -> Random:
    """A `Random` determined entirely by `(seed, label)`."""
    digest = blake2b(f"{seed}:{label}".encode(), digest_size=8).digest()
    return Random(int.from_bytes(digest, "big"))


def weighted_pick[T](rng: Random, items: Sequence[T], weights: Sequence[int]) -> T:
    """Pick one item with integer weights. `random.choices` uses floats; this does not."""
    if len(items) != len(weights):
        raise ValueError("items and weights must be the same length")
    total = sum(weights)
    if total <= 0:
        raise ValueError("weights must sum to a positive integer")
    draw = rng.randrange(total)
    for item, weight in zip(items, weights, strict=True):
        draw -= weight
        if draw < 0:
            return item
    raise AssertionError("unreachable: weights exhausted")


def digest_hex(seed: int, label: str, length: int) -> str:
    """Stable hex token of `length` characters, for record ids that need an opaque suffix."""
    size = (length + 1) // 2
    return blake2b(f"{seed}:{label}".encode(), digest_size=size).hexdigest()[:length]

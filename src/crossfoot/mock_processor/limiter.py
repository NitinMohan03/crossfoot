"""Token-bucket rate limiting, with an injectable clock.

`Retry-After` is the whole reason this exists: the Phase 2.2 client backs off on the number this
module computes, so the number has to be honest -- waiting that long must actually work. The
clock is a parameter so a test can assert exactly that without sleeping.

The floats here are clock seconds, not money. No amount passes through this module.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class Allowed:
    pass


@dataclass(frozen=True, slots=True)
class RateLimited:
    """Refused, with the seconds to put in `Retry-After`. Always a positive integer."""

    retry_after_seconds: int


Verdict = Allowed | RateLimited


class TokenBucket:
    """`burst` requests immediately, then `rate_per_second` sustained, per key."""

    def __init__(self, rate_per_second: int, burst: int, clock: Clock = time.monotonic) -> None:
        if rate_per_second < 1 or burst < 1:
            raise ValueError("rate_per_second and burst must both be >= 1")
        self._rate = rate_per_second
        self._burst = burst
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> Verdict:
        now = self._clock()
        tokens, last = self._state.get(key, (float(self._burst), now))
        tokens = min(float(self._burst), tokens + (now - last) * self._rate)
        if tokens >= 1.0:
            self._state[key] = (tokens - 1.0, now)
            return Allowed()
        self._state[key] = (tokens, now)
        # Round up, and never answer 0: a client told to wait 0 seconds will just be refused again.
        return RateLimited(retry_after_seconds=max(1, math.ceil((1.0 - tokens) / self._rate)))

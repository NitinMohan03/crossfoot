"""Runtime configuration for the mock processor API."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DEFAULT_DATA_DIR: Final = Path("data/full")
DEFAULT_TOKEN: Final = "dev-processor-token"
# Generous enough that a legitimate pagination walk never trips it, tight enough that a client
# with no backoff at all will.
DEFAULT_RATE_PER_SECOND: Final = 20
DEFAULT_BURST: Final = 40
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 500


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Everything the app needs that is not a record."""

    data_dir: Path = DEFAULT_DATA_DIR
    token: str = DEFAULT_TOKEN
    rate_per_second: int = DEFAULT_RATE_PER_SECOND
    burst: int = DEFAULT_BURST
    default_limit: int = DEFAULT_LIMIT
    max_limit: int = MAX_LIMIT

    @classmethod
    def from_env(cls, data_dir: Path | None = None) -> "ApiSettings":
        """Read the `CROSSFOOT_PROCESSOR_*` variables, falling back to the defaults above."""
        env_dir = os.environ.get("CROSSFOOT_PROCESSOR_DATA")
        resolved = data_dir or (Path(env_dir) if env_dir else DEFAULT_DATA_DIR)
        return cls(
            data_dir=resolved,
            token=os.environ.get("CROSSFOOT_PROCESSOR_TOKEN") or DEFAULT_TOKEN,
            rate_per_second=_int_env("CROSSFOOT_PROCESSOR_RATE", DEFAULT_RATE_PER_SECOND),
            burst=_int_env("CROSSFOOT_PROCESSOR_BURST", DEFAULT_BURST),
        )


def _int_env(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value

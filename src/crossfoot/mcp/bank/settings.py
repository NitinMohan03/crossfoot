"""Runtime configuration for `mcp-bank`. Rule 4: a frozen dataclass with `from_env()`.

Two sources are named here because the server reads two: the bank export itself, which is
files, and the counterparty list its descriptions are resolved against, which lives in the ERP
(see `mcp/common/parties.py` for why that is Postgres and not a copy on disk).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from crossfoot.db.connect import readonly_database_url
from crossfoot.mcp.erp.settings import DEFAULT_SCHEMA, IDENTIFIER

#: A dataset root, as `just seed` writes one. The export is its `bank/` directory.
DEFAULT_DATA: Final = Path("data/full")
BANK_SUBDIR: Final = "bank"
SIDECAR_NAME: Final = "posted_at.jsonl"

DEFAULT_LIMIT: Final = 50
#: The agent sees at most 10 candidates (CLAUDE.md), so this bound is for ingestion, not prompts.
MAX_LIMIT: Final = 200
CONNECT_TIMEOUT: Final = 5

DATA_ENV: Final = "CROSSFOOT_BANK_DATA"
DSN_ENV: Final = "CROSSFOOT_BANK_DSN"
ERP_SCHEMA_ENV: Final = "CROSSFOOT_BANK_ERP_SCHEMA"
MAX_LIMIT_ENV: Final = "CROSSFOOT_BANK_MAX_LIMIT"


@dataclass(frozen=True, slots=True)
class BankSettings:
    """Everything the server needs that is not a record.

    The DSN defaults to the read-only role, as `mcp-erp`'s does: the only thing this server asks
    Postgres for is the counterparty names and the confirmed aliases.
    """

    data_dir: Path = DEFAULT_DATA
    dsn: str = field(default_factory=readonly_database_url)
    erp_schema: str = DEFAULT_SCHEMA
    default_limit: int = DEFAULT_LIMIT
    max_limit: int = MAX_LIMIT
    connect_timeout: int = CONNECT_TIMEOUT

    def __post_init__(self) -> None:
        if not IDENTIFIER.match(self.erp_schema):
            raise ValueError(f"{self.erp_schema!r} is not a usable schema name")
        if self.default_limit < 1 or self.max_limit < self.default_limit:
            raise ValueError("limits must satisfy 1 <= default_limit <= max_limit")

    @property
    def bank_dir(self) -> Path:
        """Where the monthly CSVs and the `posted_at` sidecar are."""
        return self.data_dir / BANK_SUBDIR

    @classmethod
    def from_env(
        cls,
        data_dir: str | Path | None = None,
        dsn: str | None = None,
        erp_schema: str | None = None,
    ) -> "BankSettings":
        """Read `CROSSFOOT_BANK_*`, falling back to the defaults above."""
        return cls(
            data_dir=Path(data_dir or os.environ.get(DATA_ENV) or DEFAULT_DATA),
            dsn=dsn or os.environ.get(DSN_ENV) or readonly_database_url(),
            erp_schema=erp_schema or os.environ.get(ERP_SCHEMA_ENV) or DEFAULT_SCHEMA,
            max_limit=_int_env(MAX_LIMIT_ENV, MAX_LIMIT),
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

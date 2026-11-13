"""Runtime configuration for `mcp-processor`. Rule 4: a frozen dataclass with `from_env()`.

Two sources are named here because the server reads two: the processor's REST API, which is the
records, and the ERP's counterparty list its payer names are resolved against, which is Postgres
(`mcp/common/parties.py` says why that is not a copy).

**The token.** It comes from `CROSSFOOT_PROCESSOR_TOKEN` -- the same variable `just proc-api`
reads, so one secret has one name -- and from nowhere else. There is no command-line flag for
it, because a process list is a log. The field is `repr=False`, so a settings object that ends
up in a traceback or a debug print does not carry it, and the checks below refuse a bad token
without quoting it.

The default is the mock API's own development token, so the server runs against `just proc-api`
with no configuration. It is restated here rather than imported: a client that imported its
server's settings would not be a client. A test pins the two equal.
"""

import os
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from crossfoot.db.connect import readonly_database_url
from crossfoot.mcp.erp.settings import DEFAULT_SCHEMA, IDENTIFIER

#: Where `just proc-api` serves by default.
DEFAULT_URL: Final = "http://127.0.0.1:8099"
DEFAULT_TOKEN: Final = "dev-processor-token"

DEFAULT_LIMIT: Final = 50
#: The agent sees at most 10 candidates (CLAUDE.md), so this bound is for ingestion, not prompts.
MAX_LIMIT: Final = 200
#: Seconds one request may take before the source counts as not answering.
TIMEOUT: Final = 5.0
#: How many times a 429 is waited out before the call gives up. See `client.py`.
MAX_RETRIES: Final = 2
#: The longest `Retry-After` this server will sleep through, in seconds.
MAX_RETRY_WAIT: Final = 5.0
CONNECT_TIMEOUT: Final = 5

URL_ENV: Final = "CROSSFOOT_PROCESSOR_URL"
TOKEN_ENV: Final = "CROSSFOOT_PROCESSOR_TOKEN"
DSN_ENV: Final = "CROSSFOOT_PROCESSOR_DSN"
ERP_SCHEMA_ENV: Final = "CROSSFOOT_PROCESSOR_ERP_SCHEMA"
MAX_LIMIT_ENV: Final = "CROSSFOOT_PROCESSOR_MAX_LIMIT"


def _clean_base_url(value: str) -> str:
    """The API's base URL without a trailing slash, or a refusal that says what is wrong."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{value!r} is not an http(s) URL with a host")
    if parts.username is not None or parts.password is not None:
        # Credentials in a URL end up in logs and error messages; the token has its own field.
        raise ValueError("the processor URL must not carry credentials")
    if parts.query or parts.fragment:
        raise ValueError(f"{value!r} must be a base URL, without a query or a fragment")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class ProcessorSettings:
    """Everything the server needs that is not a record."""

    base_url: str = DEFAULT_URL
    token: str = field(default=DEFAULT_TOKEN, repr=False)
    dsn: str = field(default_factory=readonly_database_url, repr=False)
    erp_schema: str = DEFAULT_SCHEMA
    default_limit: int = DEFAULT_LIMIT
    max_limit: int = MAX_LIMIT
    timeout: float = TIMEOUT
    max_retries: int = MAX_RETRIES
    max_retry_wait: float = MAX_RETRY_WAIT
    connect_timeout: int = CONNECT_TIMEOUT

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _clean_base_url(self.base_url))
        # Printable ASCII with no spaces: anything else cannot be a bearer token, and a line
        # break in one would be a second header. Refused without repeating the value.
        if not self.token or not all(33 <= ord(char) <= 126 for char in self.token):
            raise ValueError(f"{TOKEN_ENV} is empty or is not a usable bearer token")
        if not IDENTIFIER.match(self.erp_schema):
            raise ValueError(f"{self.erp_schema!r} is not a usable schema name")
        if self.default_limit < 1 or self.max_limit < self.default_limit:
            raise ValueError("limits must satisfy 1 <= default_limit <= max_limit")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.max_retries < 0 or self.max_retry_wait < 0:
            raise ValueError("max_retries and max_retry_wait must not be negative")

    @classmethod
    def from_env(
        cls,
        base_url: str | None = None,
        dsn: str | None = None,
        erp_schema: str | None = None,
    ) -> "ProcessorSettings":
        """Read `CROSSFOOT_PROCESSOR_*`, falling back to the defaults above."""
        return cls(
            base_url=base_url or os.environ.get(URL_ENV) or DEFAULT_URL,
            token=os.environ.get(TOKEN_ENV) or DEFAULT_TOKEN,
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

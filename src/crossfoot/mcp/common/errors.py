"""The one error envelope every MCP server answers with.

| code               | cause                                                              |
| ------------------ | ------------------------------------------------------------------ |
| `not_found`        | no record with that id                                             |
| `invalid_argument` | an argument did not parse, or a range runs backwards               |
| `limit_exceeded`   | a page size above the server's maximum                             |
| `unresolved_vendor`| a counterparty name entity resolution could not pin to one party   |
| `unavailable`      | the source could not be reached                                    |
| `malformed_source` | the source answered, with something that does not parse            |
| `read_only`        | the call would have written, which no tool may do                  |

`unavailable` and `malformed_source` are different problems with different remedies: the first
is worth a retry, the second is not -- a bank export with a row that will not parse stays that
way until a person looks at the file. Neither is the caller's `invalid_argument`. A
`malformed_source` says where it stopped in its extras: `file` and `row` for a statement,
`record` for a settlement line, `file`, `page` and `row` for a remittance advice.

`unavailable` may say more about itself in its extras. `retryable: false` marks the one kind
that waiting does not cure -- a service that refused this server's own credential -- and
`retry_after` is the number of seconds a rate-limiting service asked to be left alone for.

Errors are returned as a payload, not raised. Rule 2 in `crossfoot.mcp`: the agent needs a
stable code to branch on, and the SDK turns an unexpected exception into "Error executing tool
X", which tells it nothing. `guard` is that rule as a function, shared so that every server
answers a failure the same way.
"""

from collections.abc import Callable
from typing import Any, Final

NOT_FOUND: Final = "not_found"
INVALID_ARGUMENT: Final = "invalid_argument"
LIMIT_EXCEEDED: Final = "limit_exceeded"
UNRESOLVED_VENDOR: Final = "unresolved_vendor"
UNAVAILABLE: Final = "unavailable"
MALFORMED_SOURCE: Final = "malformed_source"
READ_ONLY: Final = "read_only"

ERROR_CODES: Final = (
    NOT_FOUND,
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    UNRESOLVED_VENDOR,
    UNAVAILABLE,
    MALFORMED_SOURCE,
    READ_ONLY,
)

Payload = dict[str, Any]


class ToolError(Exception):
    """A failure with a stable code. Raised inside the tool layer, returned by the server."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> Payload:
        """The envelope, ready to be the tool's result."""
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def guard(call: Callable[[], Payload]) -> Payload:
    """Run a tool body, returning the error envelope instead of raising (rule 2)."""
    try:
        return call()
    except ToolError as exc:
        return exc.payload()

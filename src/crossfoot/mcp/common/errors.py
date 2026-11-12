"""The one error envelope every MCP server answers with.

| code               | cause                                                              |
| ------------------ | ------------------------------------------------------------------ |
| `not_found`        | no record with that id                                             |
| `invalid_argument` | an argument did not parse, or a range runs backwards               |
| `limit_exceeded`   | a page size above the server's maximum                             |
| `unresolved_vendor`| a counterparty name entity resolution could not pin to one party   |
| `unavailable`      | the source could not be reached                                    |
| `read_only`        | the call would have written, which no tool may do                  |

Errors are returned as a payload, not raised. Rule 2 in `crossfoot.mcp`: the agent needs a
stable code to branch on, and the SDK turns an unexpected exception into "Error executing tool
X", which tells it nothing.
"""

from typing import Any, Final

NOT_FOUND: Final = "not_found"
INVALID_ARGUMENT: Final = "invalid_argument"
LIMIT_EXCEEDED: Final = "limit_exceeded"
UNRESOLVED_VENDOR: Final = "unresolved_vendor"
UNAVAILABLE: Final = "unavailable"
READ_ONLY: Final = "read_only"

ERROR_CODES: Final = (
    NOT_FOUND,
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    UNRESOLVED_VENDOR,
    UNAVAILABLE,
    READ_ONLY,
)


class ToolError(Exception):
    """A failure with a stable code. Raised inside the tool layer, returned by the server."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        """The envelope, ready to be the tool's result."""
        return {"error": {"code": self.code, "message": self.message, **self.extra}}

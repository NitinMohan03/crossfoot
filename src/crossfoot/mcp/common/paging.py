"""The page-size check every server applies to a `limit` argument.

It was written once in `mcp-erp` and copied into `mcp-bank` and `mcp-processor`; the fourth
server is where a copy becomes a rule, so it lives here. A missing `limit` is the server's
default; one below 1 is the caller's `invalid_argument`; one above the server's maximum is
`limit_exceeded`, which carries `max_limit` so the caller can ask again without guessing.
"""

from crossfoot.mcp.common.errors import INVALID_ARGUMENT, LIMIT_EXCEEDED, ToolError


def page_limit(limit: int | None, *, default: int, maximum: int) -> int:
    """The page size a call gets, or a coded refusal."""
    if limit is None:
        return default
    if limit < 1:
        raise ToolError(INVALID_ARGUMENT, f"limit must be at least 1, got {limit}")
    if limit > maximum:
        raise ToolError(
            LIMIT_EXCEEDED,
            f"limit {limit} is above this server's maximum of {maximum}",
            max_limit=maximum,
        )
    return limit

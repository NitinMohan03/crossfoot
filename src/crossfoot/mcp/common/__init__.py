"""What the servers genuinely share: the error envelope and the payload shape."""

from crossfoot.mcp.common.errors import (
    ERROR_CODES,
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    NOT_FOUND,
    READ_ONLY,
    UNAVAILABLE,
    UNRESOLVED_VENDOR,
    ToolError,
)
from crossfoot.mcp.common.results import party_payload, record_payload, records_payload

__all__ = [
    "ERROR_CODES",
    "INVALID_ARGUMENT",
    "LIMIT_EXCEEDED",
    "NOT_FOUND",
    "READ_ONLY",
    "UNAVAILABLE",
    "UNRESOLVED_VENDOR",
    "ToolError",
    "party_payload",
    "record_payload",
    "records_payload",
]

"""What the servers genuinely share: the error envelope and the payload shape."""

from crossfoot.mcp.common.errors import (
    ERROR_CODES,
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    MALFORMED_SOURCE,
    NOT_FOUND,
    READ_ONLY,
    UNAVAILABLE,
    UNRESOLVED_VENDOR,
    Payload,
    ToolError,
    guard,
)
from crossfoot.mcp.common.results import party_payload, record_payload, records_payload

__all__ = [
    "ERROR_CODES",
    "INVALID_ARGUMENT",
    "LIMIT_EXCEEDED",
    "MALFORMED_SOURCE",
    "NOT_FOUND",
    "READ_ONLY",
    "UNAVAILABLE",
    "UNRESOLVED_VENDOR",
    "Payload",
    "ToolError",
    "guard",
    "party_payload",
    "record_payload",
    "records_payload",
]

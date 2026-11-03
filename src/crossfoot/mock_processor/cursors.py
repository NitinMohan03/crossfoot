"""Opaque pagination cursors.

A cursor is base64url over `<collection>|<date>|<id>|<digest>`, where the digest is keyed. The
client therefore cannot compute a cursor, guess an offset, tamper with one, or replay a cursor
from one collection against another -- which is the point: the Phase 2.2 client has to carry the
token it was handed, exactly as it would against a real processor.
"""

import base64
import binascii
import hmac
from datetime import date
from hashlib import blake2b
from typing import Final

# Not a secret. It exists so a cursor is opaque and self-consistent, not to protect anything.
CURSOR_KEY: Final = b"crossfoot-mock-processor-cursor-v1"
FIELDS: Final = 4


class BadCursor(ValueError):
    """The cursor was not one we issued."""


def _digest(payload: str) -> str:
    return blake2b(payload.encode("utf-8"), key=CURSOR_KEY, digest_size=8).hexdigest()


def encode(collection: str, key: tuple[date, str]) -> str:
    sort_date, record_id = key
    payload = f"{collection}|{sort_date.isoformat()}|{record_id}"
    token = f"{payload}|{_digest(payload)}".encode()
    return base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")


def decode(collection: str, cursor: str) -> tuple[date, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        token = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise BadCursor("cursor is not valid base64url") from exc

    parts = token.split("|")
    if len(parts) != FIELDS:
        raise BadCursor("cursor is malformed")
    name, raw_date, record_id, digest = parts
    if not hmac.compare_digest(digest, _digest(f"{name}|{raw_date}|{record_id}")):
        raise BadCursor("cursor digest does not match")
    if name != collection:
        raise BadCursor(f"cursor belongs to {name!r}, not {collection!r}")
    try:
        return date.fromisoformat(raw_date), record_id
    except ValueError as exc:
        raise BadCursor("cursor carries an unparseable date") from exc

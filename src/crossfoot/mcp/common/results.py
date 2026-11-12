"""Canonical records to tool payloads.

`mode="json"` so dates leave as ISO strings and integers leave as integers -- minor units must
survive the trip as numbers a client cannot mistake for a rounded decimal. `exclude_none` keeps
the payload small, which matters when ten of these go into a prompt.
"""

from collections.abc import Sequence
from typing import Any

from crossfoot.models.canonical import CanonicalRecord, PartyRef


def record_payload(record: CanonicalRecord) -> dict[str, Any]:
    return record.model_dump(mode="json", exclude_none=True)


def records_payload(records: Sequence[CanonicalRecord], **extra: Any) -> dict[str, Any]:
    """A list result: the records, their count, and whatever the tool wants to say about them."""
    return {
        "records": [record_payload(record) for record in records],
        "count": len(records),
        **extra,
    }


def party_payload(party: PartyRef, **extra: Any) -> dict[str, Any]:
    return {"party": party.model_dump(mode="json", exclude_none=True), **extra}

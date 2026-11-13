"""The API's JSON, read strictly. What `parse.py` is to the bank server.

Three decisions, all of them about not guessing:

* **Models are strict and closed.** A settlement line or a batch is validated from its JSON text
  with `strict=True` and `extra="forbid"`: an amount written `1.0` or `"100"`, a missing field,
  or a field this client has never heard of is `malformed_source`. The last is deliberate -- a
  new `refund_minor` would otherwise be dropped without a word, and this is money.
* **No amount is ever a float.** Minor units are validated as JSON integers straight from the
  text. `fx_rate` is a string in the source and stays one until `canonical.py` turns it into
  integer parts per million.
* **A record keeps its own text.** `split_page` hands back each record of a list response as the
  exact slice of the body it occupied, so `CanonicalRecord.raw` is what the source wrote and
  not this client's re-serialisation of it. Re-serialising would mean choosing a key order and a
  number format on the source's behalf.

Messages name the field and the problem, never the offending value: a body that is not what was
documented may contain anything.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError

#: Spec 01 §5.3 and §3.3. A record whose id is not one of these is not this API's record.
SETTLEMENT_ID: Final = r"^proc:stl_[0-9a-f]{12}$"
BATCH_ID: Final = r"^bat_\d{4}-\d{2}-\d{2}_\d{2,}$"

SettlementId = Annotated[str, Field(pattern=SETTLEMENT_ID)]
BatchId = Annotated[str, Field(pattern=BATCH_ID)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

#: The three keys of a list response, and no others.
PAGE_KEYS: Final = frozenset({"data", "next_cursor", "has_more"})
WHITESPACE: Final = " \t\n\r"
#: How many field problems one message lists before it stops.
MAX_PROBLEMS: Final = 3

_ID_IN_TEXT: Final = re.compile(r'"id"\s*:\s*"((?:proc:stl|bat)_[0-9A-Za-z_\-]{1,40})"')


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


# `parse_float=Decimal` so that a number this module only walks past is still never a float.
_DECODER: Final = json.JSONDecoder(parse_float=Decimal, parse_constant=_refuse_constant)


class Wire(BaseModel):
    """Immutable, strict about types, and loud about fields it does not know."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class SettlementLine(Wire):
    """One settlement line as the API writes it (spec 01 §3.3)."""

    id: SettlementId
    batch_id: BatchId
    product: Literal["card", "pay_by_bank"]
    payer_name: str
    payer_reference: str | None
    currency: Currency
    gross_minor: int
    fee_minor: int
    net_minor: int
    #: Foreign units per GBP, six decimal places, as text. None on a sterling line.
    fx_rate: str | None
    fx_rate_date: date | None
    #: Kept as text: the mapper needs the original beside the UTC moment it parses out of it.
    created_at: str
    settled_on: date


class BatchLine(Wire):
    """One payout batch as the API writes it.

    `total_net_minor` is in `currency`; `payout_base_minor` is the sterling that reached the
    bank, which for a foreign batch is that total converted at the payout-date rate.
    """

    id: BatchId
    closed_on: date
    paid_out_on: date
    currency: Currency
    count: int
    total_net_minor: int
    payout_base_minor: int


@dataclass(frozen=True, slots=True)
class Page:
    """One list response: each record's own text, and how to ask for the next page."""

    records: tuple[str, ...]
    next_cursor: str | None
    has_more: bool


def _id_in(raw: str) -> str | None:
    """The record's id, if the text has one where an id goes. Best effort, for the message."""
    found = _ID_IN_TEXT.search(raw)
    return None if found is None else found.group(1)


def _parse[Model: Wire](model: type[Model], raw: str, what: str) -> Model:
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or 'record'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        shown = "; ".join(problems[:MAX_PROBLEMS])
        more = len(problems) - MAX_PROBLEMS
        extra: dict[str, Any] = {}
        record_id = _id_in(raw)
        if record_id is not None:
            extra["record"] = record_id
        raise ToolError(
            MALFORMED_SOURCE,
            f"the processor API sent a {what} that is not the documented shape: {shown}"
            + (f" (and {more} more)" if more > 0 else ""),
            **extra,
        ) from exc


def parse_settlement(raw: str) -> SettlementLine:
    """One settlement line from its JSON text, or `malformed_source` naming the field."""
    return _parse(SettlementLine, raw, "settlement line")


def parse_batch(raw: str) -> BatchLine:
    """One batch from its JSON text, or `malformed_source` naming the field."""
    return _parse(BatchLine, raw, "batch")


def decode_body(body: bytes) -> str:
    """A response body as text. Strict UTF-8: mojibake in a payer name resolves to someone else."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(
            MALFORMED_SOURCE,
            f"the processor API sent a body that is not UTF-8 (byte at offset {exc.start})",
        ) from exc


def _skip(text: str, index: int) -> int:
    while index < len(text) and text[index] in WHITESPACE:
        index += 1
    return index


def _expect(text: str, index: int, char: str) -> int:
    """The position after `char`, which must be at `index`, with whitespace skipped after it."""
    if index >= len(text) or text[index] != char:
        raise ValueError(f"expected {char!r} at offset {index}")
    return _skip(text, index + 1)


def _record_spans(text: str, index: int) -> tuple[tuple[str, ...], int]:
    """The elements of the JSON array starting at `index`, each as the text it was written as."""
    index = _expect(text, index, "[")
    spans: list[str] = []
    if index < len(text) and text[index] == "]":
        return (), index + 1
    while True:
        value, end = _DECODER.raw_decode(text, index)
        if not isinstance(value, dict):
            raise ValueError("`data` holds something that is not a record")
        spans.append(text[index:end])
        index = _skip(text, end)
        if index < len(text) and text[index] == ",":
            index = _skip(text, index + 1)
            continue
        return tuple(spans), _expect(text, index, "]")


def _split(text: str) -> Page:
    fields: dict[str, Any] = {}
    index = _expect(text, _skip(text, 0), "{")
    while True:
        key, end = _DECODER.raw_decode(text, index)
        if not isinstance(key, str) or key in fields:
            raise ValueError("a key is repeated or is not a string")
        index = _expect(text, _skip(text, end), ":")
        if key == "data":
            fields[key], index = _record_spans(text, index)
        else:
            fields[key], index = _DECODER.raw_decode(text, index)
        index = _skip(text, index)
        if index < len(text) and text[index] == ",":
            index = _skip(text, index + 1)
            continue
        index = _expect(text, index, "}")
        break
    if index != len(text):
        raise ValueError("text after the closing brace")

    if set(fields) != PAGE_KEYS:
        raise ValueError(f"its keys are not exactly {', '.join(sorted(PAGE_KEYS))}")
    next_cursor, has_more = fields["next_cursor"], fields["has_more"]
    if not isinstance(has_more, bool):
        raise ValueError("`has_more` is not a boolean")
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
        raise ValueError("`next_cursor` is neither null nor a cursor")
    if has_more != (next_cursor is not None):
        # Believing either one would end a walk early or never end it.
        raise ValueError("`has_more` and `next_cursor` disagree")
    return Page(records=fields["data"], next_cursor=next_cursor, has_more=has_more)


def split_page(text: str) -> Page:
    """A list response into its records' own text and its cursor, or `malformed_source`."""
    try:
        return _split(text)
    except (ValueError, IndexError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ToolError(
            MALFORMED_SOURCE,
            f"the processor API sent a list that is not the documented shape: {detail}",
        ) from exc

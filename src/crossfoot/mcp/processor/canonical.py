"""Settlement lines to canonical records. Rule 3: the only place that knows how the processor
spells things.

Five processor-specific decisions live here and nowhere else:

* **The amount is the gross.** `amount.minor` is what the payer paid; the fee the processor
  kept and the net it passed on sit in `detail`, in the same currency. `fee + net == gross` is
  checked on the record, so a line whose own arithmetic is off never reaches the matcher.
* **The sterling view uses the line's own rate.** A foreign line carries the settlement-date
  rate as text (`"1.333209"`, foreign units per GBP). It becomes integer parts per million --
  the representation `ref.fx_rates` stores -- and `base_minor` is the gross divided by it,
  rounded half up, in `Decimal`. That is the rate the processor recorded, not the one the bank
  used: a foreign batch reaches the bank converted at the *payout*-date rate, which is why the
  bank's `TFR` amount is on the batch (`payout_base_minor`) and is not a sum of these.
* **`settled_on` stays a date, `created_at` becomes UTC.** The settlement date is a calendar
  date and is copied untouched. The creation moment is the only thing converted, and its
  original text is kept beside it. A timestamp with no offset is refused: assuming UTC would be
  right for this API and silently wrong for the next one.
* **The payer's reference is what the payer typed.** `payer_reference` has a field of its own,
  but its content is an invoice ref, a fragment of one, or the payer's own PO number
  (spec 01 §3.3). So it goes to `reference_surface`, as on a bank line, and `reference` stays
  empty: nothing here has been checked against a ledger.
* **A batch is not a record.** It is not an obligation, a payment or an advice -- the payment is
  the bank's `TFR` line, and the batch is what ties that line to these. Its id is on every
  line's `detail`; its own facts leave through `get_batch` in their wire shape.

The party is resolved by the caller through `crossfoot.entities` and handed in; nothing here
compares names.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from pydantic import ValidationError

from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError
from crossfoot.mcp.processor.wire import SettlementLine
from crossfoot.models.canonical import (
    BASE_CURRENCY,
    CanonicalRecord,
    Money,
    PartyRef,
    ProcessorDetail,
)
from crossfoot.money import round_half_up

SOURCE = "processor"
PPM: Final = 1_000_000

Resolver = Callable[[str], PartyRef]


def _malformed(line: SettlementLine, problem: str) -> ToolError:
    return ToolError(MALFORMED_SOURCE, f"settlement line {line.id}: {problem}", record=line.id)


def rate_ppm(line: SettlementLine) -> int | None:
    """The line's rate as integer parts per million, or None for a sterling line."""
    if line.fx_rate is None:
        if line.currency != BASE_CURRENCY:
            raise _malformed(line, f"a {line.currency} line carries no fx_rate")
        return None
    if line.currency == BASE_CURRENCY:
        # Said here, by name: converting sterling to sterling would only surface later as an
        # amount that does not equal itself.
        raise _malformed(line, f"a {BASE_CURRENCY} line carries an fx_rate")
    try:
        scaled = Decimal(line.fx_rate) * PPM
    except InvalidOperation as exc:
        raise _malformed(line, "fx_rate is not a decimal number") from exc
    if not scaled.is_finite() or scaled <= 0 or scaled != scaled.to_integral_value():
        raise _malformed(line, "fx_rate is not a positive rate with at most six decimal places")
    return int(scaled)


def to_base_minor(minor: int, ppm: int) -> int:
    """A foreign amount in GBP pence at `ppm` foreign units per GBP, rounded half up."""
    return round_half_up(Decimal(minor) * PPM / Decimal(ppm))


def created(line: SettlementLine) -> datetime:
    """`created_at` as a UTC moment. Its original text stays on the record beside it."""
    try:
        moment = datetime.fromisoformat(line.created_at)
    except ValueError as exc:
        raise _malformed(line, "created_at is not an ISO-8601 timestamp") from exc
    if moment.utcoffset() is None:
        raise _malformed(line, "created_at has no UTC offset, and none is assumed")
    return moment.astimezone(UTC)


def _problems(exc: ValidationError) -> str:
    errors: list[Any] = exc.errors(include_input=False, include_url=False)
    return "; ".join(str(error["msg"]).removeprefix("Value error, ") for error in errors)


def settlement_record(line: SettlementLine, party: PartyRef, raw: str) -> CanonicalRecord:
    """One settlement line in the canonical shape. `raw` is the line as the API wrote it."""
    ppm = rate_ppm(line)
    moment = created(line)
    try:
        return CanonicalRecord(
            record_id=line.id,
            source="processor",
            role="payment",
            amount=Money(
                minor=line.gross_minor,
                currency=line.currency,
                base_minor=(
                    line.gross_minor if ppm is None else to_base_minor(line.gross_minor, ppm)
                ),
                fx_rate_ppm=ppm,
                fx_rate_date=line.fx_rate_date,
            ),
            party=party,
            record_date=line.settled_on,
            event_time=moment,
            event_time_source=line.created_at,
            reference_surface=line.payer_reference or None,
            detail=ProcessorDetail(
                batch_id=line.batch_id,
                product=line.product,
                fee_minor=line.fee_minor,
                net_minor=line.net_minor,
            ),
            raw=raw,
        )
    except ValidationError as exc:
        # The canonical schema's own checks: fee + net == gross, no fee on pay_by_bank, no rate
        # on a sterling amount, a rate and its date together. The source broke one of them.
        raise _malformed(line, _problems(exc)) from exc

"""The bank export's text to rows. Strict wherever a mistake could move money or an id.

The format is spec 01 §3.2 and nothing more exotic: comma-separated, six columns, no quoting,
one header line. The plan's "fixed-width/CSV, encoding quirks" names a kind of source; what the
generator emits is plain CSV, so that is what this parses, and a row that is not that is refused
by name rather than guessed at.

**A bank line's id is its position.** `bank:YYYY-MM-DD:NNNNNN` is the value date plus the row's
1-based number within its month file -- there is no id column. So a row is never skipped: a
skipped row would hide a payment *and* renumber every line after it. A month with one bad row
answers `malformed_source`, naming the file and the row; the other months keep answering.

The running `Balance` is checked for the same reason. Each row's balance must be the previous
row's plus what came in less what went out, which makes the column the file's own checksum of
row order: a deleted, duplicated or re-sorted row breaks the chain at the point it happened.

Line endings are the one tolerance. The spec says CRLF; a file that passed through a tool which
rewrote them to LF holds the same rows in the same order, so both are read.

This parser is deliberately not `crossfoot.generate.read.read_bank`. That one is the
generator's own reader and the test suite's oracle; a server that shared it could not be checked
against it.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, Literal, cast, get_args

from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError

HEADER: Final = "Transaction Date,Transaction Type,Description,Paid Out,Paid In,Balance"
COLUMNS: Final = 6
DESCRIPTION_MAX: Final = 32

TxnType = Literal["FPI", "BGC", "TFR", "FPO"]
TXN_TYPES: Final[tuple[str, ...]] = get_args(TxnType)

_DATE: Final = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
#: Two decimal places, always. `12.5` or `1,250.00` is a different export, not a rounding choice.
_AMOUNT: Final = re.compile(r"^(\d+)\.(\d{2})$")
_BALANCE: Final = re.compile(r"^(-?)(\d+)\.(\d{2})$")


@dataclass(frozen=True, slots=True)
class BankRow:
    """One statement line, as the export states it. Amounts are integer minor units."""

    line_id: str
    month: str
    row: int
    value_date: date
    txn_type: TxnType
    description: str
    paid_in_minor: int
    paid_out_minor: int
    balance_minor: int
    #: The CSV row exactly as written, without its line ending.
    raw: str

    @property
    def amount_minor(self) -> int:
        """Signed: money in is positive, money out negative (the canonical convention)."""
        return self.paid_in_minor - self.paid_out_minor


@dataclass(frozen=True, slots=True)
class PostedAt:
    """When the bank posted a line: the moment in UTC, and the local text it was written as."""

    moment: datetime
    source: str


def line_id(value_date: date, row: int) -> str:
    """The id spec 01 §5.3 gives a bank line, which is also what the manifest calls it."""
    return f"bank:{value_date.isoformat()}:{row:06d}"


def _lines(text: str) -> list[str]:
    """Split on LF and drop one trailing CR per line. Not `splitlines`, which also splits on
    form feeds and Unicode separators that a description could legitimately contain."""
    rows = [row.removesuffix("\r") for row in text.split("\n")]
    if rows and rows[-1] == "":
        rows.pop()
    return rows


def _minor(whole: str, pence: str) -> int:
    """`4417`, `00` -> `441700`. Integers from digits; no float is ever constructed."""
    return int(whole) * 100 + int(pence)


def parse_month(month: str, text: str) -> tuple[BankRow, ...]:
    """Every row of one month's statement, in file order."""
    name = f"{month}.csv"

    def bad(row: int, why: str) -> ToolError:
        where = f"{name} row {row}" if row else name
        return ToolError(MALFORMED_SOURCE, f"{where}: {why}", file=name, row=row)

    rows = _lines(text)
    if not rows or rows[0] != HEADER:
        raise bad(0, "the header is not the six columns of a statement export")

    parsed: list[BankRow] = []
    for number, raw in enumerate(rows[1:], 1):
        fields = raw.split(",")
        if len(fields) != COLUMNS:
            raise bad(number, f"expected {COLUMNS} fields, found {len(fields)}")
        day_text, txn_type, description, paid_out, paid_in, balance = fields

        day = _DATE.match(day_text)
        if day is None:
            raise bad(number, f"{day_text!r} is not a DD/MM/YYYY date")
        try:
            value_date = date(int(day.group(3)), int(day.group(2)), int(day.group(1)))
        except ValueError as exc:
            raise bad(number, f"{day_text!r} is not a real date") from exc
        if f"{value_date.year:04d}-{value_date.month:02d}" != month:
            raise bad(number, f"{day_text} does not belong in the {month} statement")

        if txn_type not in TXN_TYPES:
            raise bad(number, f"unknown transaction type {txn_type!r}")
        if len(description) > DESCRIPTION_MAX:
            raise bad(number, f"description is {len(description)} characters, over the bank's 32")

        if bool(paid_out) == bool(paid_in):
            raise bad(number, "exactly one of Paid Out and Paid In must be filled")
        amount = _AMOUNT.match(paid_in or paid_out)
        if amount is None:
            raise bad(number, f"{paid_in or paid_out!r} is not an amount with two decimal places")
        moved = _minor(amount.group(1), amount.group(2))

        stated = _BALANCE.match(balance)
        if stated is None:
            raise bad(number, f"{balance!r} is not a balance with two decimal places")
        balance_minor = _minor(stated.group(2), stated.group(3))
        if stated.group(1):
            balance_minor = -balance_minor

        row = BankRow(
            line_id=line_id(value_date, number),
            month=month,
            row=number,
            value_date=value_date,
            txn_type=cast(TxnType, txn_type),
            description=description,
            paid_in_minor=moved if paid_in else 0,
            paid_out_minor=moved if paid_out else 0,
            balance_minor=balance_minor,
            raw=raw,
        )
        if parsed:
            expected = parsed[-1].balance_minor + row.amount_minor
            if row.balance_minor != expected:
                raise bad(
                    number,
                    "the running balance does not follow from the row above "
                    f"(states {row.balance_minor}, expected {expected} minor units); "
                    "a row is missing, repeated or out of order",
                )
            if row.value_date < parsed[-1].value_date:
                raise bad(number, "value dates run backwards")
        parsed.append(row)
    return tuple(parsed)


def parse_sidecar(name: str, text: str) -> dict[str, PostedAt]:
    """`posted_at.jsonl`: bank line id -> the moment it was posted.

    The sidecar writes `Europe/London` local time *with its offset* (`+00:00` in winter,
    `+01:00` in summer), so the conversion to UTC needs no timezone database and no guess about
    which side of a clock change a line fell on: the offset is in the text. That text is kept
    verbatim beside the converted moment, as the evidence of what was converted. A timestamp
    with no offset is refused -- it cannot be placed in UTC without assuming one.
    """

    def bad(line: int, why: str) -> ToolError:
        return ToolError(MALFORMED_SOURCE, f"{name} line {line}: {why}", file=name, row=line)

    posted: dict[str, PostedAt] = {}
    for number, raw in enumerate(_lines(text), 1):
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise bad(number, "not a JSON object") from exc
        if not isinstance(entry, dict):
            raise bad(number, "not a JSON object")
        key, source = entry.get("id"), entry.get("posted_at")
        if not isinstance(key, str) or not isinstance(source, str):
            raise bad(number, "needs a string `id` and a string `posted_at`")
        try:
            local = datetime.fromisoformat(source)
        except ValueError as exc:
            raise bad(number, f"{source!r} is not an ISO-8601 timestamp") from exc
        if local.utcoffset() is None:
            raise bad(number, f"{source!r} carries no UTC offset, so it cannot be placed in UTC")
        if key in posted:
            raise bad(number, f"{key} appears twice")
        posted[key] = PostedAt(moment=local.astimezone(UTC), source=source)
    return posted

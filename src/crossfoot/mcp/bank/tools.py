"""What the bank server can be asked, as plain Python. Rule 1: every decision lives here.

Nothing in this module imports MCP. `server.py` registers these methods and turns their results
into payloads; the tests call them directly. Failures are `ToolError`s with a stable code, which
the server returns in the one envelope.

**Months are loaded one at a time, when a call needs them.** A lookup by id reads the one file
its date names; a date window reads the months it overlaps; an export reads forward until the
page is full. So a statement with a row that will not parse fails the calls that touch that
month and no others -- the rest of the year is still on the table.

**Order is the statement's own:** month, then row. That is also the order of the ids, and the
order the keyset in `export_bank_lines` pages through.
"""

import re
from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any, Final

from crossfoot.entities import resolve_description
from crossfoot.mcp.bank import canonical
from crossfoot.mcp.bank.files import ReadOnlyExport
from crossfoot.mcp.bank.parse import BankRow, PostedAt, parse_month, parse_sidecar
from crossfoot.mcp.bank.parse import line_id as make_line_id
from crossfoot.mcp.bank.settings import SIDECAR_NAME, BankSettings
from crossfoot.mcp.common.errors import INVALID_ARGUMENT, NOT_FOUND, ToolError
from crossfoot.mcp.common.paging import page_limit
from crossfoot.mcp.common.parties import ErpParties, Parties
from crossfoot.mcp.erp.tools import parse_date
from crossfoot.models.canonical import CanonicalRecord, PartyRef

#: `bank:2024-03-15:000004`, with or without the source prefix.
LINE_ID: Final = re.compile(r"^(?:bank:)?(\d{4})-(\d{2})-(\d{2}):(\d{6})$")


def _ordered(low: Any, high: Any, field: str) -> None:
    if low is not None and high is not None and low > high:
        raise ToolError(INVALID_ARGUMENT, f"{field}: {low!r} is after {high!r}")


def _month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def parse_line_id(value: str, field: str = "id") -> tuple[date, int]:
    """A bank line id to its value date and row number, or a coded refusal."""
    match = LINE_ID.match(value.strip())
    if match is None:
        raise ToolError(
            INVALID_ARGUMENT, f"{field}: {value!r} is not a bank line id (bank:YYYY-MM-DD:NNNNNN)"
        )
    year, month, day, row = (int(part) for part in match.groups())
    try:
        return date(year, month, day), row
    except ValueError as exc:
        raise ToolError(
            INVALID_ARGUMENT, f"{field}: {value!r} names a date that is not real"
        ) from exc


class BankService:
    """The bank export, read-only, with the names its descriptions are resolved against."""

    def __init__(
        self,
        settings: BankSettings,
        parties: Parties | None = None,
        export: ReadOnlyExport | None = None,
    ) -> None:
        self.settings = settings
        self.export = export or ReadOnlyExport(settings.bank_dir, SIDECAR_NAME)
        self.parties: Parties = parties or ErpParties(
            settings.dsn, settings.erp_schema, connect_timeout=settings.connect_timeout
        )
        self._months: dict[str, tuple[BankRow, ...]] = {}
        self._posted: dict[str, PostedAt] | None = None
        self._resolved: dict[str, PartyRef] = {}

    # -- plumbing ---------------------------------------------------------------------------

    def limit(self, limit: int | None) -> int:
        return page_limit(
            limit, default=self.settings.default_limit, maximum=self.settings.max_limit
        )

    def _month(self, month: str) -> tuple[BankRow, ...] | None:
        """One month's rows, parsed once. None when the export has no file for that month."""
        if month not in self._months:
            text = self.export.month_text(month)
            if text is None:
                return None
            self._months[month] = parse_month(month, text)
        return self._months[month]

    def _rows(self, months: Sequence[str]) -> Iterator[BankRow]:
        """Rows of the given months, in statement order, each month read only when reached."""
        for month in months:
            yield from self._month(month) or ()

    def _posted_at(self, line_id: str) -> PostedAt | None:
        if self._posted is None:
            text = self.export.sidecar_text()
            # An export without the sidecar is a plain bank export: it has no timestamps, and a
            # record with no `event_time` says exactly that.
            self._posted = {} if text is None else parse_sidecar(SIDECAR_NAME, text)
        return self._posted.get(line_id)

    def _resolve(self, description: str) -> PartyRef:
        """Entity resolution, once per distinct description: the same text is the same party."""
        if description not in self._resolved:
            self._resolved[description] = resolve_description(
                description, self.parties.name_index(), self.parties.registry()
            )
        return self._resolved[description]

    def _record(self, row: BankRow) -> CanonicalRecord:
        party = canonical.party_for(row, self._resolve)
        return canonical.bank_record(row, party, self._posted_at(row.line_id))

    # -- the agent-facing tools -------------------------------------------------------------

    def get_bank_line(self, line_id: str) -> CanonicalRecord:
        """One statement line by id (`bank:2024-03-15:000004`, with or without the prefix)."""
        if not line_id.strip():
            raise ToolError(INVALID_ARGUMENT, "id is empty")
        value_date, number = parse_line_id(line_id)
        rows = self._month(_month_of(value_date))
        wanted = make_line_id(value_date, number)
        # The row number is the position in the month file; the date must agree with it, or the
        # id names a line that was never written.
        if rows is None or not 1 <= number <= len(rows) or rows[number - 1].line_id != wanted:
            raise ToolError(NOT_FOUND, f"no bank line with id {wanted!r}")
        return self._record(rows[number - 1])

    def find_bank_lines(
        self,
        amount_min_minor: int | None = None,
        amount_max_minor: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = None,
    ) -> tuple[Sequence[CanonicalRecord], int]:
        """Statement lines in an amount and/or value-date window, in statement order.

        Amounts are **signed** GBP minor units, as on the record: a receipt is positive and a
        refund negative, so `amount_min_minor=1` asks for money in. Returns the records and the
        number of lines that matched in all, which is larger than the page when the window is
        wider than `limit`.
        """
        _ordered(amount_min_minor, amount_max_minor, "amount range")
        first, last = parse_date(date_from, "date_from"), parse_date(date_to, "date_to")
        _ordered(first, last, "date range")
        page = self.limit(limit)

        months = [
            month
            for month in self.export.months()
            if (first is None or month >= _month_of(first))
            and (last is None or month <= _month_of(last))
        ]
        matched = [
            row
            for row in self._rows(months)
            if (first is None or row.value_date >= first)
            and (last is None or row.value_date <= last)
            and (amount_min_minor is None or row.amount_minor >= amount_min_minor)
            and (amount_max_minor is None or row.amount_minor <= amount_max_minor)
        ]
        return [self._record(row) for row in matched[:page]], len(matched)

    def export_bank_lines(
        self, after_id: str | None = None, limit: int | None = None
    ) -> Sequence[CanonicalRecord]:
        """The bulk read ingestion uses: every line in statement order, keyset-paged on id."""
        page = self.limit(limit)
        after: tuple[str, int] | None = None
        if after_id is not None and after_id.strip():
            value_date, number = parse_line_id(after_id, "after_id")
            after = (_month_of(value_date), number)
        months = [month for month in self.export.months() if after is None or month >= after[0]]
        records: list[CanonicalRecord] = []
        for row in self._rows(months):
            if after is not None and (row.month, row.row) <= after:
                continue
            records.append(self._record(row))
            if len(records) == page:
                break
        return records

    def close(self) -> None:
        self.parties.close()

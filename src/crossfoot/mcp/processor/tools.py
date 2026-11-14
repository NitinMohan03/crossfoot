"""What the processor server can be asked, as plain Python. Rule 1: every decision lives here.

Nothing in this module imports MCP. `server.py` registers these methods and turns their results
into payloads; the tests call them directly. Failures are `ToolError`s with a stable code, which
the server returns in the one envelope.

**Nothing is cached but the names.** Every call asks the API, so what the agent reads is what
the processor says now, and a rate limit is met honestly instead of being hidden behind a copy.
Payer names are resolved once per distinct name, as on the bank server: the same text is the
same party.

**An argument is checked before a request is built.** An id that is not an id, a date that is
not a date, a range that runs backwards and a page above the maximum are all answered here, with
no request sent. That is also what keeps caller text out of a URL path: only a string that
matched the id pattern is ever put in one.

**The source is checked against itself.** The record that comes back must be the record that was
asked for; a line in a date window must be inside the window; a batch's lines must number its
`count` and their nets must add up to its `total_net_minor`. Each of those failing is
`malformed_source`: the API answered, and the answer cannot be right.

**Order is the API's:** settlement date, then id; for batches, closing date, then id.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from crossfoot.entities import resolve_description
from crossfoot.mcp.common.errors import (
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    MALFORMED_SOURCE,
    ToolError,
)
from crossfoot.mcp.common.paging import page_limit
from crossfoot.mcp.common.parties import ErpParties, Parties
from crossfoot.mcp.erp.tools import parse_date
from crossfoot.mcp.processor import canonical
from crossfoot.mcp.processor.client import ProcessorApi
from crossfoot.mcp.processor.settings import ProcessorSettings
from crossfoot.mcp.processor.wire import (
    BatchLine,
    SettlementLine,
    parse_batch,
    parse_settlement,
)
from crossfoot.models.canonical import CanonicalRecord, PartyRef

#: `proc:stl_7f3a09c1d2e4`, with or without the source prefix (spec 01 §5.3).
SETTLEMENT_ID: Final = re.compile(r"^(?:proc:)?(stl_[0-9a-f]{12})$")
#: `bat_2024-03-14_02`. Matched after lower-casing: the bank's `TFR` line writes it upper case.
BATCH_ID: Final = re.compile(r"^bat_\d{4}-\d{2}-\d{2}_\d{2,}$")
#: Longer than any cursor the API issues; a longer one is not a cursor.
MAX_CURSOR: Final = 512
#: How many pages of one settlement day `get_batch` reads before it stops looking.
MAX_BATCH_PAGES: Final = 20


@dataclass(frozen=True, slots=True)
class Settlements:
    """One page of settlement lines, and how to ask for the next."""

    records: tuple[CanonicalRecord, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class Batches:
    """One page of payout batches, and how to ask for the next."""

    batches: tuple[BatchLine, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class BatchWithLines:
    """A payout batch, the lines it paid out (up to the page asked for), and how many it has."""

    batch: BatchLine
    records: tuple[CanonicalRecord, ...]
    total_lines: int


def _ordered(low: Any, high: Any, field: str) -> None:
    if low is not None and high is not None and low > high:
        raise ToolError(INVALID_ARGUMENT, f"{field}: {low!r} is after {high!r}")


def parse_settlement_id(value: str, field: str = "id") -> str:
    """A settlement id in its full form, `proc:stl_...`, or a coded refusal."""
    if not value.strip():
        raise ToolError(INVALID_ARGUMENT, f"{field} is empty")
    match = SETTLEMENT_ID.match(value.strip())
    if match is None:
        raise ToolError(
            INVALID_ARGUMENT, f"{field}: {value!r} is not a settlement id (proc:stl_<12 hex>)"
        )
    return f"proc:{match.group(1)}"


def parse_batch_id(value: str, field: str = "id") -> str:
    """A batch id as the processor writes it, lower case, or a coded refusal."""
    if not value.strip():
        raise ToolError(INVALID_ARGUMENT, f"{field} is empty")
    wanted = value.strip().lower()
    if BATCH_ID.match(wanted) is None:
        raise ToolError(
            INVALID_ARGUMENT, f"{field}: {value!r} is not a batch id (bat_YYYY-MM-DD_NN)"
        )
    return wanted


def parse_cursor(value: str | None) -> str | None:
    """A cursor to hand back to the API unread, or None to start from the beginning."""
    if value is None or not value.strip():
        return None
    if len(value) > MAX_CURSOR:
        raise ToolError(INVALID_ARGUMENT, "cursor: longer than any cursor the processor API issues")
    return value.strip()


class ProcessorService:
    """The processor API, read-only, with the names its payers are resolved against."""

    def __init__(
        self,
        settings: ProcessorSettings,
        parties: Parties | None = None,
        api: ProcessorApi | None = None,
    ) -> None:
        self.settings = settings
        self.api = api or ProcessorApi(settings)
        self.parties: Parties = parties or ErpParties(
            settings.dsn, settings.erp_schema, connect_timeout=settings.connect_timeout
        )
        self._resolved: dict[str, PartyRef] = {}

    # -- plumbing ---------------------------------------------------------------------------

    def limit(self, limit: int | None) -> int:
        return page_limit(
            limit, default=self.settings.default_limit, maximum=self.settings.max_limit
        )

    def _resolve(self, payer_name: str) -> PartyRef:
        """Entity resolution, once per distinct name.

        `resolve_description`, not `resolve`: measured on this slice's 552 lines it gives one
        wrong counterparty where `resolve` gives seven
        (`tests/test_entities/test_processor_payers.py`).
        """
        if payer_name not in self._resolved:
            self._resolved[payer_name] = resolve_description(
                payer_name, self.parties.name_index(), self.parties.registry()
            )
        return self._resolved[payer_name]

    def _record(self, line: SettlementLine, raw: str) -> CanonicalRecord:
        return canonical.settlement_record(line, self._resolve(line.payer_name), raw)

    def _records(self, raws: Sequence[str]) -> tuple[CanonicalRecord, ...]:
        """Every line of a page, or none of it: a page with a hole in it would look whole."""
        return tuple(self._record(parse_settlement(raw), raw) for raw in raws)

    # -- the agent-facing tools -------------------------------------------------------------

    def get_settlement(self, settlement_id: str) -> CanonicalRecord:
        """One settlement line by id (`proc:stl_7f3a09c1d2e4`, with or without the prefix)."""
        wanted = parse_settlement_id(settlement_id)
        raw = self.api.settlement(wanted)
        line = parse_settlement(raw)
        if line.id != wanted:
            raise ToolError(
                MALFORMED_SOURCE,
                f"asked the processor API for {wanted} and was sent a different record",
                record=wanted,
            )
        return self._record(line, raw)

    def list_settlements(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = None,
    ) -> Settlements:
        """Settlement lines whose `settled_on` falls in a window, oldest first.

        The window is on the settlement date -- the calendar date the processor settled the
        payment -- not on the moment the payment was created, which is usually the day before.
        `has_more` says the window holds more than the page; there is no cursor here, because
        the answer to a window that is too wide is a narrower window.
        """
        first, last = parse_date(date_from, "date_from"), parse_date(date_to, "date_to")
        _ordered(first, last, "date range")
        page = self.api.settlements(limit=self.limit(limit), settled_from=first, settled_to=last)
        records = self._records(page.records)
        for record in records:
            day = record.record_date
            if (first is not None and day < first) or (last is not None and day > last):
                raise ToolError(
                    MALFORMED_SOURCE,
                    f"the processor API sent {record.record_id}, settled on {day.isoformat()}, "
                    "for a window that does not contain that date",
                    record=record.record_id,
                )
        return Settlements(records=records, next_cursor=None, has_more=page.has_more)

    def get_batch(self, batch_id: str, limit: int | None = None) -> BatchWithLines:
        """A payout batch and the settlement lines it paid out.

        The bank sees a batch as one `TFR` line (`PAYCO SETTLE BAT_2024-03-14_02`); this is what
        is inside it. The id is accepted as the bank writes it. `limit` bounds the lines
        returned, never the lines checked: every line of the batch is read, counted and summed
        against the batch's own `count` and `total_net_minor` before anything is answered.
        """
        wanted = parse_batch_id(batch_id)
        page_size = self.limit(limit)
        batch = parse_batch(self.api.batch(wanted))
        if batch.id != wanted:
            raise ToolError(
                MALFORMED_SOURCE,
                f"asked the processor API for {wanted} and was sent a different batch",
                record=wanted,
            )
        members = self._batch_lines(batch)
        return BatchWithLines(
            batch=batch,
            records=tuple(self._record(line, raw) for line, raw in members[:page_size]),
            total_lines=len(members),
        )

    def _batch_lines(self, batch: BatchLine) -> list[tuple[SettlementLine, str]]:
        """Every line of a batch, checked against the batch's own totals.

        The API has no filter on `batch_id`. A batch is one settlement date and one currency,
        so its lines are that day's lines carrying its id: at most a few pages.
        """
        day: date = batch.closed_on
        members: list[tuple[SettlementLine, str]] = []
        cursor: str | None = None
        for _ in range(MAX_BATCH_PAGES):
            page = self.api.settlements(
                limit=self.settings.max_limit, cursor=cursor, settled_from=day, settled_to=day
            )
            for raw in page.records:
                line = parse_settlement(raw)
                if line.batch_id == batch.id:
                    members.append((line, raw))
            if not page.has_more:
                break
            cursor = page.next_cursor
        else:
            raise ToolError(
                LIMIT_EXCEEDED,
                f"{day.isoformat()} has more settlement lines than this server will read to "
                f"assemble one batch ({MAX_BATCH_PAGES} pages of {self.settings.max_limit})",
                max_limit=self.settings.max_limit,
            )

        problem: str | None = None
        if len(members) != batch.count:
            problem = f"says it holds {batch.count} lines and {len(members)} carry its id"
        elif sum(line.net_minor for line, _ in members) != batch.total_net_minor:
            problem = "has a total_net_minor that its lines' net amounts do not add up to"
        elif any(line.currency != batch.currency for line, _ in members):
            problem = f"is a {batch.currency} batch holding a line in another currency"
        if problem is not None:
            raise ToolError(MALFORMED_SOURCE, f"batch {batch.id} {problem}", record=batch.id)
        return members

    def export_settlements(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Settlements:
        """The bulk read ingestion uses: every line in the API's order, paged by its cursor.

        The cursor is the API's own and is opaque: it is handed back exactly as it was given.
        """
        page = self.api.settlements(limit=self.limit(limit), cursor=parse_cursor(cursor))
        return Settlements(
            records=self._records(page.records),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    def export_batches(self, cursor: str | None = None, limit: int | None = None) -> Batches:
        """Every payout batch in the API's order, paged by its cursor.

        Ingestion needs these beside the lines: `payout_base_minor` is the sterling that reached
        the bank, and for a foreign batch nothing on its lines adds up to it.
        """
        page = self.api.batches(limit=self.limit(limit), cursor=parse_cursor(cursor))
        return Batches(
            batches=tuple(parse_batch(raw) for raw in page.records),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    def close(self) -> None:
        self.parties.close()

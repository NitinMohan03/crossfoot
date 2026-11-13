"""What the ERP server can be asked, as plain Python. Rule 1: every decision lives here.

Nothing in this module imports MCP. `server.py` registers these methods and turns their results
into payloads; the tests call them directly. Failures are `ToolError`s with a stable code, which
the server returns in the one envelope.
"""

from collections.abc import Sequence
from datetime import date
from typing import Any, Final

import psycopg

from crossfoot.config import Thresholds
from crossfoot.entities import SELECT_SQL, AliasRegistry, Candidate, NameIndex, resolve
from crossfoot.mcp.common.errors import (
    INVALID_ARGUMENT,
    NOT_FOUND,
    UNRESOLVED_VENDOR,
    ToolError,
)
from crossfoot.mcp.common.paging import page_limit
from crossfoot.mcp.erp import canonical, queries
from crossfoot.mcp.erp.db import ReadOnlyDatabase
from crossfoot.mcp.erp.settings import ErpSettings
from crossfoot.models.canonical import CanonicalRecord, PartyRef

#: Spelled out because `list_open_invoices` is the name plan §3.3 gives the tool, and the name
#: promises something the ERP cannot know. Said once here, repeated in the tool description.
NO_STATUS_NOTE: Final = (
    "the ERP is the obligation ledger and carries no payment status, so every invoice in the "
    "window is returned; whether one is settled is what reconciliation decides"
)


def parse_date(value: str | None, field: str) -> date | None:
    """ISO date or a coded error. Dates arrive as strings so a typo is an answer, not a crash."""
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(
            INVALID_ARGUMENT, f"{field}: {value!r} is not an ISO date (YYYY-MM-DD)"
        ) from exc


def _ordered(low: Any, high: Any, field: str) -> None:
    if low is not None and high is not None and low > high:
        raise ToolError(INVALID_ARGUMENT, f"{field}: {low!r} is after {high!r}")


class ErpService:
    """The ERP source, read-only, with its name index and alias registry.

    The index and the registry are loaded once and kept: the ERP dump is static within a run,
    and the registry is frozen -- it never learns here, whatever happens downstream.
    """

    def __init__(self, settings: ErpSettings, db: ReadOnlyDatabase | None = None) -> None:
        self.settings = settings
        self.db = db or ReadOnlyDatabase(settings.dsn, connect_timeout=settings.connect_timeout)
        self._index: NameIndex | None = None
        self._registry: AliasRegistry | None = None
        self._registry_available = True

    # -- plumbing ---------------------------------------------------------------------------

    def _rows_or_none(self, query: tuple[Any, tuple[Any, ...]]) -> list[dict[str, Any]] | None:
        """Rows, or None when the relation does not exist -- which is a different problem."""
        statement, params = query
        with self.db.cursor() as cur:
            try:
                cur.execute(statement, params)
            except psycopg.errors.UndefinedTable:
                return None
            return [dict(row) for row in cur.fetchall()]

    def _rows(self, query: tuple[Any, tuple[Any, ...]]) -> list[dict[str, Any]]:
        rows = self._rows_or_none(query)
        if rows is None:
            raise ToolError(
                NOT_FOUND,
                f"schema {self.settings.schema!r} has no ERP tables; run `just db-load data/full`",
            )
        return rows

    def limit(self, limit: int | None) -> int:
        return page_limit(
            limit, default=self.settings.default_limit, maximum=self.settings.max_limit
        )

    def name_index(self) -> NameIndex:
        if self._index is None:
            rows = self._rows(queries.counterparties(self.settings.schema))
            self._index = NameIndex(
                Candidate(
                    counterparty_id=int(row["id"]),
                    legal_name=str(row["legal_name"]),
                    trading_name=str(row["trading_name"]),
                )
                for row in rows
            )
        return self._index

    def registry(self) -> AliasRegistry:
        """The alias registry, frozen. A server never learns; only a confirmed escalation does."""
        if self._registry is None:
            rows = self._rows_or_none((SELECT_SQL, ()))
            if rows is None:
                # Degrade rather than fail every call: resolution still has its other layers,
                # and `lookup_alias` reports that the registry was not there.
                self._registry_available = False
                self._registry = AliasRegistry.empty()
            else:
                self._registry = AliasRegistry.from_pairs(
                    (str(row["alias_norm"]), int(row["counterparty_id"])) for row in rows
                )
        return self._registry

    @property
    def registry_available(self) -> bool:
        self.registry()
        return self._registry_available

    def resolve_vendor(self, vendor: str) -> PartyRef:
        """A free-text vendor name to one counterparty, or a coded refusal."""
        party = resolve(vendor, self.name_index(), self.registry())
        if party.counterparty_id is None:
            raise ToolError(
                UNRESOLVED_VENDOR,
                f"{vendor!r} does not identify one counterparty "
                f"(best similarity {party.similarity}, threshold {Thresholds.NAME_SIM_MIN})",
                best_similarity=party.similarity,
                threshold=Thresholds.NAME_SIM_MIN,
            )
        return party

    # -- the agent-facing tools -------------------------------------------------------------

    def get_invoice(self, ref: str) -> CanonicalRecord:
        """One invoice by its ERP ref (`INV-2024-0891`, with or without the `erp:` prefix)."""
        wanted = ref.removeprefix("erp:").strip()
        if not wanted:
            raise ToolError(INVALID_ARGUMENT, "ref is empty")
        rows = self._rows(queries.invoice_by_ref(self.settings.schema, wanted))
        if not rows:
            raise ToolError(NOT_FOUND, f"no invoice with ref {wanted!r}")
        return canonical.invoice_record(rows[0])

    def list_open_invoices(
        self,
        vendor: str | None = None,
        amount_min_minor: int | None = None,
        amount_max_minor: int | None = None,
        issued_from: str | None = None,
        issued_to: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        limit: int | None = None,
    ) -> tuple[Sequence[CanonicalRecord], PartyRef | None]:
        """Invoices for a vendor and/or an amount and date window.

        Amounts are GBP minor units and filter on `base_amount_minor`, so one window works
        across currencies. Returns the records and the resolution of `vendor`, which the server
        passes back so the agent can see *which* counterparty it got.
        """
        _ordered(amount_min_minor, amount_max_minor, "amount range")
        issued = (parse_date(issued_from, "issued_from"), parse_date(issued_to, "issued_to"))
        due = (parse_date(due_from, "due_from"), parse_date(due_to, "due_to"))
        _ordered(*issued, "issue date range")
        _ordered(*due, "due date range")

        party = self.resolve_vendor(vendor) if vendor else None
        rows = self._rows(
            queries.invoices(
                self.settings.schema,
                counterparty_id=None if party is None else party.counterparty_id,
                amount_min_minor=amount_min_minor,
                amount_max_minor=amount_max_minor,
                issued_from=issued[0],
                issued_to=issued[1],
                due_from=due[0],
                due_to=due[1],
                limit=self.limit(limit),
            )
        )
        return [canonical.invoice_record(row) for row in rows], party

    def get_credit_notes(
        self, vendor: str, limit: int | None = None
    ) -> tuple[Sequence[CanonicalRecord], PartyRef]:
        """Credit notes for one vendor. Amounts are negative: a credit note reduces what is owed."""
        party = self.resolve_vendor(vendor)
        assert party.counterparty_id is not None
        rows = self._rows(
            queries.credit_notes(
                self.settings.schema,
                counterparty_id=party.counterparty_id,
                limit=self.limit(limit),
            )
        )
        return [canonical.credit_note_record(row) for row in rows], party

    def lookup_alias(self, name: str) -> PartyRef:
        """Resolve a name through the whole ladder, reporting how it was identified.

        The shared registry tool of plan §3.3. It never writes: an alias is learned only from a
        human-confirmed escalation, and this registry is frozen.
        """
        if not name.strip():
            raise ToolError(INVALID_ARGUMENT, "name is empty")
        return resolve(name, self.name_index(), self.registry())

    def export_invoices(
        self, after_ref: str | None = None, limit: int | None = None
    ) -> Sequence[CanonicalRecord]:
        """The bulk read ingestion uses: invoices in ref order, keyset-paged on `after_ref`."""
        rows = self._rows(
            queries.invoices(
                self.settings.schema,
                after_ref=(after_ref or "").removeprefix("erp:").strip() or None,
                limit=self.limit(limit),
            )
        )
        return [canonical.invoice_record(row) for row in rows]

    def close(self) -> None:
        self.db.close()

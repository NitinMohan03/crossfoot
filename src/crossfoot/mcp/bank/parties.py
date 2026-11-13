"""Where the bank server gets the counterparties its descriptions are resolved against.

A bank statement names payers; it does not know who the business's counterparties are. That
list is the ERP's, and the confirmed aliases are `er.counterparty_alias`, both in Postgres. So
this server reads files for its records and asks Postgres for exactly two things: the
counterparty names and the alias registry.

**Why not a copy of the names on disk beside the CSVs?** Because it would be a second source of
truth. With one list, a bank line's `party` and `mcp-erp`'s `lookup_alias` on the same text
cannot disagree -- they ran the same resolver over the same rows. A file-side copy would be
right until the day someone reloaded one and not the other, and nothing would say so.

The access is `mcp-erp`'s own: the `crossfoot_ro` role, read-only transactions, the registry
constructed frozen. Nothing new can write, and nothing here learns.

**When Postgres is not there, the call fails** with `unavailable`. The alternative -- serving the
lines with every party unresolved -- would look like an answer. Ingestion would store it, and a
few thousand payments would then read as coming from nobody.

`Parties` is a protocol so the parsing and failure tests can hand the service a fixed index and
run without a database. `mcp-processor` will be the third thing to need these names; that is the
point at which this belongs in `common/`, and not before.
"""

from typing import Any, Protocol

import psycopg

from crossfoot.entities import SELECT_SQL, AliasRegistry, Candidate, NameIndex
from crossfoot.mcp.common.errors import UNAVAILABLE, ToolError
from crossfoot.mcp.erp import queries
from crossfoot.mcp.erp.db import ReadOnlyDatabase


class Parties(Protocol):
    """The two things entity resolution needs."""

    def name_index(self) -> NameIndex: ...

    def registry(self) -> AliasRegistry: ...

    def close(self) -> None: ...


class StaticParties:
    """A fixed index and registry. For tests, and for anything that already holds the names."""

    def __init__(self, index: NameIndex, registry: AliasRegistry | None = None) -> None:
        self._index = index
        self._registry = registry or AliasRegistry.empty()

    def name_index(self) -> NameIndex:
        return self._index

    def registry(self) -> AliasRegistry:
        return self._registry

    def close(self) -> None:
        return None


class ErpParties:
    """Counterparties and aliases from the ERP database, loaded once, read-only.

    Loaded once because the ERP dump is static within a run and the registry is frozen: a
    resolution made at the start of an export and one made at the end must agree.
    """

    def __init__(
        self,
        dsn: str,
        schema: str,
        *,
        connect_timeout: int = 5,
        db: ReadOnlyDatabase | None = None,
    ) -> None:
        self._schema = schema
        self._db = db or ReadOnlyDatabase(dsn, connect_timeout=connect_timeout)
        self._index: NameIndex | None = None
        self._registry: AliasRegistry | None = None

    def _rows(self, query: tuple[Any, tuple[Any, ...]]) -> list[dict[str, Any]] | None:
        """Rows, or None when the relation does not exist."""
        statement, params = query
        with self._db.cursor() as cur:
            try:
                cur.execute(statement, params)
            except psycopg.errors.UndefinedTable:
                return None
            return [dict(row) for row in cur.fetchall()]

    def name_index(self) -> NameIndex:
        if self._index is None:
            rows = self._rows(queries.counterparties(self._schema))
            if rows is None:
                raise ToolError(
                    UNAVAILABLE,
                    f"schema {self._schema!r} has no counterparties to resolve payer names "
                    "against; run `just db-load data/full`",
                )
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
            rows = self._rows((SELECT_SQL, ()))
            # No registry table is a database nobody has confirmed an alias in yet, which is
            # the same thing as an empty registry. `mcp-erp` reads it that way too.
            self._registry = AliasRegistry.from_pairs(
                (str(row["alias_norm"]), int(row["counterparty_id"])) for row in rows or []
            )
        return self._registry

    def close(self) -> None:
        self._db.close()

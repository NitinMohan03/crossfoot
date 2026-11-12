"""The ERP server's read-only Postgres access.

Two guards, because one is a promise and two is a boundary:

* the DSN names `crossfoot_ro`, a role holding SELECT and nothing else
  (`crossfoot.db.roles`), so a write fails at the server;
* every transaction is opened read-only, so a misconfigured DSN pointing at the owner would
  still refuse a write with `read_only_sql_transaction`.

The connection is lazy and reconnects once if the server has been idle long enough for Postgres
to have dropped it -- a stdio server lives as long as the agent does. A source that cannot be
reached becomes `unavailable`, never a traceback: rule 2.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from crossfoot.mcp.common.errors import READ_ONLY, UNAVAILABLE, ToolError


class ReadOnlyDatabase:
    """A lazily-opened, read-only Postgres connection."""

    def __init__(self, dsn: str, *, connect_timeout: int = 5) -> None:
        self._dsn = dsn
        self._timeout = connect_timeout
        self._conn: psycopg.Connection[Any] | None = None

    def _connect(self) -> psycopg.Connection[Any]:
        try:
            conn = psycopg.connect(self._dsn, connect_timeout=self._timeout, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            # Deliberately not echoing the DSN: it carries a password.
            raise ToolError(
                UNAVAILABLE, f"the ERP database is not reachable ({type(exc).__name__})"
            ) from exc
        # Must be set outside a transaction, which a fresh connection is.
        conn.read_only = True
        return conn

    def _live(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        return self._conn

    @contextmanager
    def cursor(self) -> Iterator[psycopg.Cursor[Any]]:
        """A cursor on a read-only transaction, rolled back on the way out.

        Rolled back rather than committed because nothing here writes, and because leaving the
        transaction open would hold a snapshot for as long as the agent keeps thinking.
        """
        conn = self._live()
        try:
            with conn.cursor() as cur:
                yield cur
        except psycopg.errors.ReadOnlySqlTransaction as exc:
            conn.rollback()
            raise ToolError(READ_ONLY, "this server's transactions are read-only") from exc
        except psycopg.errors.InsufficientPrivilege as exc:
            conn.rollback()
            raise ToolError(READ_ONLY, "the ERP role may only SELECT") from exc
        except psycopg.OperationalError as exc:
            # The connection died between calls; drop it so the next call reconnects.
            self.close()
            raise ToolError(
                UNAVAILABLE, f"the ERP database went away ({type(exc).__name__})"
            ) from exc
        except Exception:
            # Any other failure still aborted the transaction; leave it clean for the next call.
            conn.rollback()
            raise
        else:
            conn.rollback()

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

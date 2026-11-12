"""The counterparty alias registry (plan §2.7).

What makes this hard is not the lookup, it is the discipline around the write:

* It learns **only from human-confirmed escalations.** Learning from the matcher's own output
  compounds errors and makes results depend on processing order; learning from the manifest
  launders ground truth into the thing being measured. `er.counterparty_alias` CHECK-constrains
  `provenance` and requires an `escalation_id`, so neither is representable in the table, and
  `learn()` is the only function in the codebase that writes to it.
* It is **frozen during eval runs.** A registry is frozen unless something deliberately
  unfreezes it: `frozen` defaults to True, `CROSSFOOT_EVAL_RUN` forces it, and a frozen
  registry raises `RegistryFrozen` rather than quietly skipping the write. The MCP servers
  construct it frozen and connect as a role that holds SELECT and nothing else, so a bug here
  would still not reach a write.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Self

import psycopg

from crossfoot.names import normalise

#: The only provenance `er.counterparty_alias` accepts.
PROVENANCE: Final = "human_confirmed"

#: Set during an eval run. Freezes every registry in the process, however it was constructed.
EVAL_ENV: Final = "CROSSFOOT_EVAL_RUN"

TABLE: Final = "er.counterparty_alias"


class RegistryFrozen(RuntimeError):
    """A write was attempted on a frozen registry. Raised, never swallowed."""


@dataclass(frozen=True, slots=True)
class Alias:
    """One confirmed alias, as the table stores it."""

    alias_norm: str
    alias_raw: str
    counterparty_id: int
    escalation_id: str
    confirmed_by: str
    confirmed_at: datetime


def eval_run() -> bool:
    """True when this process is part of an eval run, so no registry may learn."""
    return bool(os.environ.get(EVAL_ENV))


class AliasRegistry:
    """Normalised alias -> counterparty id, with a single, guarded write path."""

    def __init__(self, aliases: Mapping[str, int] | None = None, *, frozen: bool = True) -> None:
        self._aliases: dict[str, int] = dict(aliases or {})
        self._frozen = frozen

    @classmethod
    def load(cls, conn: psycopg.Connection[Any], *, frozen: bool = True) -> Self:
        """Read every confirmed alias. Frozen unless the caller is the review path."""
        with conn.cursor() as cur:
            cur.execute(f"SELECT alias_norm, counterparty_id FROM {TABLE}")
            rows = cur.fetchall()
        return cls({str(row[0]): int(row[1]) for row in rows}, frozen=frozen)

    @classmethod
    def empty(cls) -> Self:
        """A registry that knows nothing. What the fuzzy layer is measured against."""
        return cls({}, frozen=True)

    @property
    def frozen(self) -> bool:
        """True when this registry refuses to learn. Also true during any eval run."""
        return self._frozen or eval_run()

    def __len__(self) -> int:
        return len(self._aliases)

    def lookup(self, name: str) -> int | None:
        """The counterparty a surface name is a confirmed alias of, if any."""
        key = normalise(name)
        if not key:
            return None
        return self._aliases.get(key)

    def learn(
        self,
        conn: psycopg.Connection[Any],
        *,
        alias_raw: str,
        counterparty_id: int,
        escalation_id: str,
        confirmed_by: str,
        confirmed_at: datetime | None = None,
    ) -> Alias:
        """Record one human-confirmed alias. The only write path in the system.

        `conn` must be a privileged connection: the read-only role the MCP servers use cannot
        execute this statement, which is the point of having a second layer.
        """
        if self.frozen:
            raise RegistryFrozen(
                "this registry is frozen"
                + (f" ({EVAL_ENV} is set)" if eval_run() else "")
                + ": aliases are learned only from human-confirmed escalations, outside eval runs"
            )
        if not escalation_id:
            raise ValueError("an alias needs the escalation that confirmed it")
        if not confirmed_by:
            raise ValueError("an alias needs the reviewer who confirmed it")
        key = normalise(alias_raw)
        if not key:
            raise ValueError(f"{alias_raw!r} normalises to nothing and cannot be an alias")
        moment = confirmed_at or datetime.now(UTC)
        alias = Alias(
            alias_norm=key,
            alias_raw=alias_raw,
            counterparty_id=counterparty_id,
            escalation_id=escalation_id,
            confirmed_by=confirmed_by,
            confirmed_at=moment,
        )
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE} (alias_norm, alias_raw, counterparty_id, provenance, "
                "escalation_id, confirmed_by, confirmed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (alias_norm) DO UPDATE SET "
                "alias_raw = EXCLUDED.alias_raw, counterparty_id = EXCLUDED.counterparty_id, "
                "escalation_id = EXCLUDED.escalation_id, confirmed_by = EXCLUDED.confirmed_by, "
                "confirmed_at = EXCLUDED.confirmed_at",
                (
                    alias.alias_norm,
                    alias.alias_raw,
                    alias.counterparty_id,
                    PROVENANCE,
                    alias.escalation_id,
                    alias.confirmed_by,
                    alias.confirmed_at,
                ),
            )
        self._aliases[key] = counterparty_id
        return alias

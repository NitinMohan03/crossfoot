"""The registry refuses to learn unless something deliberately unfroze it.

These are the Python half of the freeze. The other half is the database: `provenance` is
CHECK-constrained and the servers connect as a role with SELECT and nothing else, both covered
in `tests/test_db/test_alias_registry.py` and `tests/test_mcp/test_no_write.py`.
"""

from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest

from crossfoot.entities import EVAL_ENV, AliasRegistry, RegistryFrozen, eval_run


def unusable_conn() -> psycopg.Connection[Any]:
    """A connection every refusal must reject before it could possibly be used."""
    return cast("psycopg.Connection[Any]", object())


def learn(registry: AliasRegistry, **over: object) -> object:
    kwargs: dict[str, Any] = {
        "alias_raw": "Alphabet Inc",
        "counterparty_id": 9,
        "escalation_id": "esc:0001",
        "confirmed_by": "reviewer@example.com",
        "confirmed_at": datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
    }
    return registry.learn(unusable_conn(), **(kwargs | over))


def test_a_registry_is_frozen_unless_asked_otherwise() -> None:
    assert AliasRegistry().frozen is True
    assert AliasRegistry.empty().frozen is True
    assert AliasRegistry(frozen=False).frozen is False


def test_a_frozen_registry_refuses_to_learn() -> None:
    with pytest.raises(RegistryFrozen, match="frozen"):
        learn(AliasRegistry())


def test_an_eval_run_freezes_a_registry_that_was_built_unfrozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AliasRegistry(frozen=False)
    assert registry.frozen is False
    monkeypatch.setenv(EVAL_ENV, "1")
    assert eval_run() is True
    assert registry.frozen is True
    with pytest.raises(RegistryFrozen, match=EVAL_ENV):
        learn(registry)


def test_an_alias_needs_the_escalation_and_the_reviewer_that_confirmed_it() -> None:
    with pytest.raises(ValueError, match="needs the escalation"):
        learn(AliasRegistry(frozen=False), escalation_id="")
    with pytest.raises(ValueError, match="needs the reviewer"):
        learn(AliasRegistry(frozen=False), confirmed_by="")


def test_an_alias_that_normalises_to_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="normalises to nothing"):
        learn(AliasRegistry(frozen=False), alias_raw="  ,,,  ")


def test_lookup_normalises_its_argument() -> None:
    registry = AliasRegistry({"ALPHABET": 9})
    assert registry.lookup("Alphabet, Inc.") == 9
    assert registry.lookup("alphabet") == 9
    assert registry.lookup("Google UK Ltd") is None
    assert registry.lookup("") is None
    assert len(registry) == 1

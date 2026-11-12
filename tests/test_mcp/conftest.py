"""The committed dev slice, loaded into a schema of its own, served by a real `mcp-erp`.

The slice is ground truth and read-only; the dump is read and its `erp.` prefix rewritten to
`erp_test.` in memory, so a test run does not clobber whatever a developer loaded from
`data/full` with `just db-load`. That rewrite is also what the server's `schema` setting is for.

Skips when Postgres is unreachable, like `tests/test_db/conftest.py` -- but a green run with
silent skips is not evidence, so run pytest with `-rs` and look.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from crossfoot.db.connect import database_url, readonly_database_url
from crossfoot.db.roles import apply_roles
from crossfoot.db.schema import apply_er
from crossfoot.mcp.erp import ErpService, ErpSettings

DEV_SLICE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "dev"
SCHEMA = "erp_test"

# What the committed slice holds. Hardcoded so a change to the slice shows up here as a failure,
# the way tests/test_db/test_reference_load.py pins its row counts.
COUNTERPARTIES = 60
INVOICES = 1422
CREDIT_NOTES = 41


def rewritten_dump() -> str:
    """The dev slice's `erp.sql`, retargeted at `erp_test`. Reads ground truth, never edits it."""
    text = (DEV_SLICE / "erp.sql").read_text(encoding="utf-8")
    text = text.replace(
        "DROP SCHEMA IF EXISTS erp CASCADE", f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"
    )
    text = text.replace("CREATE SCHEMA erp;", f"CREATE SCHEMA {SCHEMA};")
    text = re.sub(r"\berp\.", f"{SCHEMA}.", text)
    if "erp.invoices" in text or "CREATE SCHEMA erp;" in text:
        raise AssertionError("the dump still targets the `erp` schema")
    return text


@pytest.fixture(scope="session")
def owner() -> Iterator[psycopg.Connection[Any]]:
    """A privileged connection: loading a dump and granting need one."""
    try:
        live = psycopg.connect(database_url(), connect_timeout=5, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not reachable ({exc.__class__.__name__}); run `just db-up`")
    with live:
        yield live


@pytest.fixture(scope="session")
def erp_schema(owner: psycopg.Connection[Any]) -> str:
    """The dev slice in `erp_test`, with the read-only role able to read it."""
    with owner.cursor() as cur:
        cur.execute(rewritten_dump())
    apply_er(owner)
    granted = apply_roles(owner, schemas=(SCHEMA, "ref", "er"))
    assert SCHEMA in granted
    return SCHEMA


@pytest.fixture(scope="session")
def settings(erp_schema: str) -> ErpSettings:
    """The server's configuration: the read-only role, pointed at the test schema."""
    return ErpSettings(dsn=readonly_database_url(), schema=erp_schema)


@pytest.fixture
def service(settings: ErpSettings) -> Iterator[ErpService]:
    erp = ErpService(settings)
    try:
        yield erp
    finally:
        erp.close()


@pytest.fixture(scope="session")
def manifest_erp_ids() -> set[str]:
    """Every `erp:` id the ground-truth manifest knows (spec 01 §5.3)."""
    ids: set[str] = set()
    with (DEV_SLICE / "manifest.jsonl").open(encoding="utf-8") as handle:
        next(handle)  # the header line
        for line in handle:
            group = json.loads(line)
            for key in ("source_ids", "target_ids", "evidence_ids"):
                ids.update(value for value in group.get(key, []) if value.startswith("erp:"))
    return ids

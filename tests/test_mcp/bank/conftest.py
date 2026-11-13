"""The committed dev slice's bank export, served by a real `mcp-bank`.

Two kinds of service, because the server reads two sources:

* `offline` hands the service a fixed name index built from the slice's own `erp.sql`, so
  everything about *files* -- parsing, ids, the sidecar, the failure paths -- is tested with no
  database in the way.
* `bank` is the server as it actually runs: names and aliases from Postgres through the
  read-only role, against the `erp_test` schema the parent conftest loads.

The slice is ground truth and is only ever read. Tests that need a broken export build one in
`tmp_path` from a copy.
"""

import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from crossfoot.db.connect import readonly_database_url
from crossfoot.entities import Candidate, NameIndex
from crossfoot.generate.read import read_erp
from crossfoot.mcp.bank import BankService, BankSettings
from crossfoot.mcp.bank.files import ReadOnlyExport
from crossfoot.mcp.bank.parse import BankRow, PostedAt, parse_month, parse_sidecar
from crossfoot.mcp.bank.parties import StaticParties
from crossfoot.mcp.bank.settings import SIDECAR_NAME
from tests.test_mcp.conftest import DEV_SLICE

BANK_DIR = DEV_SLICE / "bank"

# What the committed slice holds. Hardcoded so a change to the slice shows up here as a failure.
MONTHS = 13
LINES = 1210
BY_TYPE = {"FPI": 664, "BGC": 268, "TFR": 268, "FPO": 10}
#: Sidecar lines written at each offset: British Summer Time, and GMT.
SUMMER, WINTER = 754, 456
#: Lines whose description ends in a reference the payer typed, and processor payout lines,
#: which all end in a batch id.
PAYER_REFERENCES, PAYOUT_REFERENCES = 385, 268
OPENING_BALANCE_MINOR = 1_250_000
CLOSING_BALANCE_MINOR = 838_163_205

#: A DSN nothing listens on, for a service that must never reach for the database.
NO_DATABASE = "postgresql://crossfoot_ro:crossfoot_ro@127.0.0.1:1/crossfoot"


@pytest.fixture(scope="session")
def export() -> ReadOnlyExport:
    return ReadOnlyExport(BANK_DIR, SIDECAR_NAME)


@pytest.fixture(scope="session")
def rows(export: ReadOnlyExport) -> tuple[BankRow, ...]:
    """Every row of every month, in statement order."""
    parsed: list[BankRow] = []
    for month in export.months():
        text = export.month_text(month)
        assert text is not None
        parsed.extend(parse_month(month, text))
    return tuple(parsed)


@pytest.fixture(scope="session")
def posted(export: ReadOnlyExport) -> dict[str, PostedAt]:
    text = export.sidecar_text()
    assert text is not None
    return parse_sidecar(SIDECAR_NAME, text)


@pytest.fixture(scope="session")
def manifest_groups() -> list[dict[str, Any]]:
    with (DEV_SLICE / "manifest.jsonl").open(encoding="utf-8") as handle:
        next(handle)  # the header line
        return [json.loads(line) for line in handle]


@pytest.fixture(scope="session")
def manifest_bank_ids(manifest_groups: list[dict[str, Any]]) -> set[str]:
    """Every `bank:` id the ground-truth manifest knows (spec 01 §5.3)."""
    return {
        value
        for group in manifest_groups
        for key in ("source_ids", "target_ids", "evidence_ids")
        for value in group.get(key, [])
        if value.startswith("bank:")
    }


@pytest.fixture(scope="session")
def static_parties() -> StaticParties:
    """The slice's counterparties read from its dump, not from Postgres."""
    erp = read_erp(DEV_SLICE / "erp.sql")
    return StaticParties(
        NameIndex(
            Candidate(counterparty_id=cp.id, legal_name=cp.legal_name, trading_name=cp.trading_name)
            for cp in erp.counterparties
        )
    )


@pytest.fixture
def offline(static_parties: StaticParties) -> BankService:
    """The dev slice's export with a fixed index: no database is touched."""
    return BankService(BankSettings(data_dir=DEV_SLICE, dsn=NO_DATABASE), parties=static_parties)


Builder = Callable[..., BankService]


@pytest.fixture
def broken(tmp_path: Path, static_parties: StaticParties) -> Builder:
    """A service over a private copy of the export, with files replaced or removed.

    `broken(**{"2024-06.csv": b"..."})` overwrites a file; a value of None deletes it. The copy
    lives in `tmp_path`; the committed slice is never written to.
    """

    def build(**files: bytes | None) -> BankService:
        target = tmp_path / "data"
        shutil.copytree(BANK_DIR, target / "bank")
        for name, content in files.items():
            path = target / "bank" / name
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
        return BankService(BankSettings(data_dir=target, dsn=NO_DATABASE), parties=static_parties)

    return build


@pytest.fixture(scope="session")
def bank_settings(erp_schema: str) -> BankSettings:
    """The server's real configuration: the slice's files, and names from `erp_test`."""
    return BankSettings(data_dir=DEV_SLICE, dsn=readonly_database_url(), erp_schema=erp_schema)


@pytest.fixture
def bank(bank_settings: BankSettings) -> Iterator[BankService]:
    service = BankService(bank_settings)
    try:
        yield service
    finally:
        service.close()

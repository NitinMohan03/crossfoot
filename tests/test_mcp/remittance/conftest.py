"""The committed dev slice's remittance advices -- the ones the parser may be developed against.

**The held-out layout is kept out of reach, not just out of mind** (spec 01 §6). One layout in
four is reserved so the parser can be measured on a page design it was never written for. Every
fixture here is built from `developed_dir`: a temporary dataset holding copies of the advices
from the *other* layouts and nothing else. No test that uses these fixtures can open a held-out
PDF, because none is there. The split is made from `remit.jsonl`, which is the generator's truth
and is allowed to know the layout; the server is not, and `developed_dir` does not even contain
that file, so everything below also shows the server needs nothing from it.

`test_held_out.py` is the one place the held-out advices are read, and it reads them to report a
number, not to change the parser.

The slice is ground truth and is only ever read. Tests that need a damaged advice build one in
`tmp_path`, or render one with the generator's own layouts.
"""

import dataclasses
import shutil
from collections.abc import Callable, Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from crossfoot.config import HELD_OUT_TEMPLATE
from crossfoot.db.connect import readonly_database_url
from crossfoot.entities import Candidate, NameIndex, resolve_description
from crossfoot.generate.emit import remit_pdf
from crossfoot.generate.read import read_erp, read_manifest, read_remit
from crossfoot.generate.records import RemitLine, Remittance
from crossfoot.mcp.common.parties import Parties, StaticParties
from crossfoot.mcp.remittance import RemittanceService, RemittanceSettings
from crossfoot.mcp.remittance.canonical import CURRENCY_OF, advice_record
from crossfoot.mcp.remittance.files import ReadOnlyAdvices
from crossfoot.mcp.remittance.fx import Rates, parse_rates
from crossfoot.mcp.remittance.layout import Page, Row, read_pages
from crossfoot.mcp.remittance.parse import Advice, parse_advice
from crossfoot.mcp.remittance.settings import FX_FILE, MAX_BYTES, MAX_PAGES, REMIT_SUBDIR
from crossfoot.models.canonical import CanonicalRecord
from tests.test_mcp.conftest import DEV_SLICE

REMIT_DIR = DEV_SLICE / REMIT_SUBDIR

# What the committed slice holds. Hardcoded so a change to the slice shows up here as a failure.
ADVICES = 525
BY_LAYOUT = {"A": 132, "B": 146, "C": 166, "D": 81}
DEVELOPED = 444
HELD_OUT = 81
#: The layouts the parser is developed against, which is every one but the held-out one.
DEVELOPED_LAYOUTS = tuple(layout for layout in BY_LAYOUT if layout != HELD_OUT_TEMPLATE)
#: Developed advices written in a currency other than sterling. All are `fx` events.
FOREIGN_DEVELOPED = {"EUR": 20, "USD": 15}

#: The advice most synthetic tests start from.
SAMPLE_REF = "PAY-2024-0088"
#: Five developed advices that between them use every developed layout and one foreign
#: currency: the dataset the failure tests break.
SMALL = ("PAY-2024-0001", "PAY-2024-0002", "PAY-2024-0003", "PAY-2024-0004", "PAY-2024-0027")

#: A DSN nothing listens on, for a service that must never reach for the database.
NO_DATABASE = "postgresql://crossfoot_ro:crossfoot_ro@127.0.0.1:1/crossfoot"
SAMPLE_NAME = f"{SAMPLE_REF}.pdf"
SAMPLE_LINES = (
    RemitLine("INV-2024-0891", 441700),
    RemitLine("INV-2024-0902", 128350, "rounding adjustment"),
    RemitLine("CN-2024-0007", -5000, "credit note"),
)
SAMPLE_NARRATIVE = "Payment reduced pending resolution. We hold CN-2025-0031 against INV-2024-0891."


def truth_rows() -> list[dict[str, Any]]:
    """`remit.jsonl`: what every advice says, held-out layout included. Tests only."""
    return read_remit(REMIT_DIR)


def is_developed(row: dict[str, Any]) -> bool:
    return bool(row["template"] != HELD_OUT_TEMPLATE)


def rendered(
    layout: str,
    *,
    lines: Sequence[RemitLine] = SAMPLE_LINES,
    currency: str = "GBP",
    narrative: str | None = SAMPLE_NARRATIVE,
    total_minor: int | None = None,
    payer: str = "ACME WIDGETS LTD",
    payment_ref: str = SAMPLE_REF,
    paid_on: date = date(2024, 3, 14),
) -> bytes:
    """An advice drawn by the generator in one of the developed layouts.

    `total_minor` may be set to something the lines do not add up to: that is how the tests get
    an advice that contradicts itself without hand-writing a PDF.
    """
    assert layout in DEVELOPED_LAYOUTS, "development tests never draw the held-out layout"
    rows = list(lines)
    return remit_pdf.render(
        Remittance(
            id=f"remit:RA-{payment_ref.removeprefix('PAY-')}",
            payment_ref=payment_ref,
            template=layout,
            payer=payer,
            paid_on=paid_on,
            currency=currency,
            total_minor=sum(row.amount_minor for row in rows)
            if total_minor is None
            else total_minor,
            lines=rows,
            narrative=narrative,
        )
    )


def pages_of(raw: bytes, name: str = SAMPLE_NAME) -> tuple[Page, ...]:
    return read_pages(name, raw, max_pages=MAX_PAGES)


def read(raw: bytes, name: str = SAMPLE_NAME) -> Advice:
    """Bytes to an advice, through both stages, as the service does it."""
    return parse_advice(name, pages_of(raw, name))


def swapped(pages: Sequence[Page], old: str, new: str, *, page: int = 1) -> tuple[Page, ...]:
    """The same pages with the first run reading `old` on `page` reading `new` instead."""
    out: list[Page] = []
    done = False
    for sheet in pages:
        rows: list[Row] = []
        for row in sheet.rows:
            runs = []
            for run in row.runs:
                if not done and sheet.number == page and run.text == old:
                    run, done = dataclasses.replace(run, text=new), True
                runs.append(run)
            rows.append(dataclasses.replace(row, runs=tuple(runs)))
        out.append(dataclasses.replace(sheet, rows=tuple(rows)))
    assert done, f"no run reading {old!r} on page {page}"
    return tuple(out)


def without_row(pages: Sequence[Page], text: str, *, page: int = 1) -> tuple[Page, ...]:
    """The same pages with the row holding a run that reads `text` taken out."""
    out: list[Page] = []
    done = False
    for sheet in pages:
        rows = []
        for row in sheet.rows:
            if not done and sheet.number == page and text in row.texts:
                done = True
                continue
            rows.append(row)
        out.append(dataclasses.replace(sheet, rows=tuple(rows)))
    assert done, f"no row holding {text!r} on page {page}"
    return tuple(out)


@pytest.fixture(scope="session")
def truth() -> list[dict[str, Any]]:
    return truth_rows()


@pytest.fixture(scope="session")
def developed(truth: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The truth rows for the advices the parser is developed against."""
    return [row for row in truth if is_developed(row)]


@pytest.fixture(scope="session")
def developed_dir(
    tmp_path_factory: pytest.TempPathFactory, developed: list[dict[str, Any]]
) -> Path:
    """A dataset holding the developed advices and the rate table -- and no `remit.jsonl`."""
    data = tmp_path_factory.mktemp("developed") / "data"
    (data / REMIT_SUBDIR).mkdir(parents=True)
    for row in developed:
        name = f"{row['payment_ref']}.pdf"
        shutil.copyfile(REMIT_DIR / name, data / REMIT_SUBDIR / name)
    shutil.copyfile(DEV_SLICE / FX_FILE, data / FX_FILE)
    return data


@pytest.fixture(scope="session")
def advices(developed_dir: Path) -> ReadOnlyAdvices:
    return ReadOnlyAdvices(developed_dir / REMIT_SUBDIR, developed_dir / FX_FILE, MAX_BYTES)


@pytest.fixture(scope="session")
def developed_pages(
    advices: ReadOnlyAdvices, developed: list[dict[str, Any]]
) -> dict[str, tuple[Page, ...]]:
    """Every developed advice as text runs, read once for the whole session."""
    out: dict[str, tuple[Page, ...]] = {}
    for row in developed:
        ref = str(row["payment_ref"])
        raw = advices.pdf_bytes(ref)
        assert raw is not None
        out[ref] = pages_of(raw, f"{ref}.pdf")
    return out


@pytest.fixture(scope="session")
def parsed(developed_pages: dict[str, tuple[Page, ...]]) -> dict[str, Advice]:
    """Every developed advice, parsed. A refusal here fails every test that asks for this."""
    return {ref: parse_advice(f"{ref}.pdf", pages) for ref, pages in developed_pages.items()}


@pytest.fixture(scope="session")
def manifest_groups() -> list[dict[str, Any]]:
    _, groups = read_manifest(DEV_SLICE / "manifest.jsonl")
    return groups


@pytest.fixture(scope="session")
def group_of(manifest_groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Remittance id -> the ground-truth group it is evidence for (spec 01 §5.1)."""
    return {
        evidence: group
        for group in manifest_groups
        for evidence in group.get("evidence_ids", [])
        if evidence.startswith("remit:")
    }


@pytest.fixture(scope="session")
def currency_of(group_of: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Remittance id -> the currency of the invoices it pays, from the ERP dump.

    `remit.jsonl` records no currency, so this is the only truth there is for the symbol the
    parser reads off the page.
    """
    invoices = read_erp(DEV_SLICE / "erp.sql").invoice_by_ref()
    out: dict[str, str] = {}
    for remit_id, group in group_of.items():
        currencies = {
            invoices[source.removeprefix("erp:")].currency
            for source in group["source_ids"]
            if source.startswith("erp:INV")
        }
        assert len(currencies) == 1, remit_id
        out[remit_id] = currencies.pop()
    return out


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


@pytest.fixture(scope="session")
def rates(advices: ReadOnlyAdvices) -> Rates:
    """The slice's daily rate table, as the server reads it."""
    text = advices.fx_text()
    assert text is not None
    return parse_rates(FX_FILE, text)


@pytest.fixture(scope="session")
def records(
    parsed: dict[str, Advice], static_parties: StaticParties, rates: Rates
) -> dict[str, CanonicalRecord]:
    """Every developed advice as a canonical record, keyed by record id.

    Built with the mapper and the resolver directly -- no service -- so a mapping test fails on
    the mapping and not on the tool around it.
    """
    out: dict[str, CanonicalRecord] = {}
    for advice in parsed.values():
        party = resolve_description(
            advice.payer, static_parties.name_index(), static_parties.registry()
        )
        currency = CURRENCY_OF[advice.symbol]
        rate = None if currency == "GBP" else rates[(advice.paid_on, currency)]
        record = advice_record(advice, party, rate)
        out[record.record_id] = record
    return out


@pytest.fixture(scope="session")
def offline(developed_dir: Path, static_parties: StaticParties) -> RemittanceService:
    """The developed advices with a fixed name index: no database is touched.

    Session-scoped on purpose. A service parses each advice once and keeps it, and nothing a
    tool does changes what a later call sees, so the tool tests share one and pay for the 444
    parses once. Tests about the remembering itself build their own.
    """
    return RemittanceService(
        RemittanceSettings(data_dir=developed_dir, dsn=NO_DATABASE), parties=static_parties
    )


Builder = Callable[..., RemittanceService]


@pytest.fixture
def broken(tmp_path: Path, static_parties: StaticParties) -> Builder:
    """A service over a small private dataset, with files replaced, added or removed.

    It starts as `SMALL` -- five developed advices and the rate table. `broken(**{"PAY-2024-
    0004.pdf": b"..."})` overwrites or adds a file; a value of None deletes one. The dataset
    lives in `tmp_path`; the committed slice is never written to.
    """

    def build(parties: Parties | None = None, **files: bytes | None) -> RemittanceService:
        target = tmp_path / "data"
        (target / REMIT_SUBDIR).mkdir(parents=True)
        for ref in SMALL:
            shutil.copyfile(REMIT_DIR / f"{ref}.pdf", target / REMIT_SUBDIR / f"{ref}.pdf")
        shutil.copyfile(DEV_SLICE / FX_FILE, target / FX_FILE)
        for name, content in files.items():
            path = target / name if name == FX_FILE else target / REMIT_SUBDIR / name
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
        return RemittanceService(
            RemittanceSettings(data_dir=target, dsn=NO_DATABASE), parties=parties or static_parties
        )

    return build


@pytest.fixture(scope="session")
def remittance_settings(erp_schema: str, developed_dir: Path) -> RemittanceSettings:
    """The server's real configuration: the developed advices, and names from `erp_test`."""
    return RemittanceSettings(
        data_dir=developed_dir, dsn=readonly_database_url(), erp_schema=erp_schema
    )


@pytest.fixture
def remittance(remittance_settings: RemittanceSettings) -> Iterator[RemittanceService]:
    service = RemittanceService(remittance_settings)
    try:
        yield service
    finally:
        service.close()

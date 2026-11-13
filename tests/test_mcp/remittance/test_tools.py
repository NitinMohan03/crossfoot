"""Each tool answers a known query correctly (plan §4.4 verification).

The tool layer is called directly -- no MCP in the way -- against the developed advices of the
committed slice (see `conftest.py`: the held-out layout is not in the dataset these tests use).
Counts are pinned so a change to the slice or to the parser shows up here.
"""

from pathlib import Path
from typing import Any

import pytest

from crossfoot.mcp.common.errors import ToolError
from crossfoot.mcp.common.parties import StaticParties
from crossfoot.mcp.remittance import RemittanceService, RemittanceSettings
from crossfoot.mcp.remittance.files import ReadOnlyAdvices
from crossfoot.mcp.remittance.tools import parse_payment_ref, parse_remittance_id
from crossfoot.models.canonical import AdviceLine, CanonicalRecord, RemittanceDetail
from tests.test_mcp.conftest import COUNTERPARTIES
from tests.test_mcp.remittance.conftest import DEVELOPED, NO_DATABASE, Builder

FIRST_ID, LAST_ID = "remit:RA-2024-0001", "remit:RA-2024-0525"
#: Distinct payer names on the developed advices: what entity resolution is asked, once each.
DISTINCT_PAYERS = 119


class Counting(ReadOnlyAdvices):
    """Read-only access that counts what it is asked for."""

    def __init__(self, source: ReadOnlyAdvices) -> None:
        super().__init__(source.directory, source._fx_path, source._max_bytes)
        self.pdfs: list[str] = []
        self.listings = 0
        self.fx = 0

    def payment_refs(self) -> tuple[str, ...]:
        self.listings += 1
        return super().payment_refs()

    def pdf_bytes(self, payment_ref: str) -> bytes | None:
        self.pdfs.append(payment_ref)
        return super().pdf_bytes(payment_ref)

    def fx_text(self) -> str | None:
        self.fx += 1
        return super().fx_text()


def walk(service: RemittanceService, limit: int) -> list[Any]:
    """The whole export, paged the way ingestion pages it."""
    pages = []
    after: str | None = None
    while True:
        page = service.export_remittances(after_id=after, limit=limit)
        pages.append(page)
        if not page.has_more:
            return pages
        after = page.next_after_id


# -- get_remittance_for_payment --------------------------------------------------------------


def test_an_advice_is_found_by_the_payment_it_advises_on(offline: RemittanceService) -> None:
    record = offline.get_remittance_for_payment("PAY-2024-0004")
    assert record.record_id == "remit:RA-2024-0004"
    assert (record.source, record.role) == ("remittance", "advice")
    assert (record.amount.minor, record.amount.currency) == (871_753, "GBP")
    assert record.record_date.isoformat() == "2024-01-19"
    assert record.reference == "PAY-2024-0004"
    assert record.free_text == "Single payment covering 3 invoices. Please allocate line by line."
    assert record.party.surface == "ORMSKIRK NURSERIES PLC"
    assert (record.party.counterparty_id, record.party.method) == (60, "exact")
    assert isinstance(record.detail, RemittanceDetail)
    # Two invoices less a credit note: the header and its lines travel together.
    assert record.detail.lines == (
        AdviceLine(invoice_ref="INV-2024-0027", amount_minor=83_600),
        AdviceLine(invoice_ref="INV-2024-0028", amount_minor=877_432),
        AdviceLine(invoice_ref="CN-2024-0001", amount_minor=-89_279, note="credit note"),
    )
    assert sum(line.amount_minor for line in record.detail.lines) == record.amount.minor


def test_a_foreign_advice_comes_with_its_sterling_view(offline: RemittanceService) -> None:
    amount = offline.get_remittance_for_payment("PAY-2024-0027").amount
    assert (amount.minor, amount.currency, amount.base_minor) == (362_936, "EUR", 316_029)
    assert (amount.fx_rate_ppm, str(amount.fx_rate_date)) == (1_148_426, "2024-02-16")


def test_whitespace_around_a_reference_is_not_part_of_it(offline: RemittanceService) -> None:
    assert offline.get_remittance_for_payment("  PAY-2024-0004\n").record_id == (
        "remit:RA-2024-0004"
    )


def test_every_developed_advice_can_be_fetched_and_is_what_ground_truth_says(
    offline: RemittanceService,
    developed: list[dict[str, Any]],
    records: dict[str, CanonicalRecord],
) -> None:
    for row in developed:
        record = offline.get_remittance_for_payment(row["payment_ref"])
        # The same record the mapper builds on its own, so the service adds nothing to it.
        assert record == records[row["id"]]
        assert record.amount.minor == row["total_minor"]


# -- get_remittance_lines --------------------------------------------------------------------


@pytest.mark.parametrize("asked", ["remit:RA-2024-0004", "RA-2024-0004", " remit:RA-2024-0004 "])
def test_lines_are_found_by_remittance_id_with_or_without_the_prefix(
    offline: RemittanceService, asked: str
) -> None:
    found = offline.get_remittance_lines(asked)
    assert (found.remittance_id, found.payment_ref) == ("remit:RA-2024-0004", "PAY-2024-0004")
    assert (found.currency, found.total_minor) == ("GBP", 871_753)
    assert [(line.invoice_ref, line.amount_minor, line.note) for line in found.lines] == [
        ("INV-2024-0027", 83_600, None),
        ("INV-2024-0028", 877_432, None),
        ("CN-2024-0001", -89_279, "credit note"),
    ]


def test_instalments_are_lines_against_the_one_invoice(offline: RemittanceService) -> None:
    found = offline.get_remittance_lines("remit:RA-2024-0006")
    assert [(line.invoice_ref, line.amount_minor, line.note) for line in found.lines] == [
        ("INV-2024-0023", 26_373, "instalment 1 of 2"),
        ("INV-2024-0023", 13_878, "instalment 2 of 2"),
    ]
    assert found.total_minor == 40_251


def test_the_lines_of_a_foreign_advice_are_in_its_own_currency(
    offline: RemittanceService,
) -> None:
    found = offline.get_remittance_lines("remit:RA-2024-0027")
    assert (found.currency, found.total_minor) == ("EUR", 362_936)
    assert [line.amount_minor for line in found.lines] == [362_936]


def test_the_lines_are_the_records_lines(
    offline: RemittanceService, developed: list[dict[str, Any]]
) -> None:
    for row in developed[:60]:
        record = offline.get_remittance_for_payment(row["payment_ref"])
        assert isinstance(record.detail, RemittanceDetail)
        found = offline.get_remittance_lines(row["id"])
        assert found.lines == record.detail.lines
        assert (found.remittance_id, found.total_minor) == (record.record_id, record.amount.minor)


def test_lines_need_neither_the_names_nor_the_rates(broken: Builder) -> None:
    # Reading what an advice says is a fact about one file. No database, and no rate table.
    service = broken(parties=Unreachable(), **{"fx_rates.csv": None})
    assert service.get_remittance_lines("remit:RA-2024-0027").currency == "EUR"
    assert len(service.get_remittance_lines("remit:RA-2024-0002").lines) == 4


class Unreachable:
    """Counterparty names that must not be asked for."""

    def name_index(self) -> Any:
        raise AssertionError("the names were asked for")

    def registry(self) -> Any:
        raise AssertionError("the registry was asked for")

    def close(self) -> None:
        return None


# -- export_remittances ----------------------------------------------------------------------


def test_the_export_is_every_developed_advice_once_in_id_order(
    offline: RemittanceService,
    developed: list[dict[str, Any]],
    group_of: dict[str, dict[str, Any]],
) -> None:
    pages = walk(offline, 200)
    assert [(len(page.records), page.has_more) for page in pages] == [
        (200, True),
        (200, True),
        (44, False),
    ]
    assert [page.next_after_id for page in pages] == [
        "remit:RA-2024-0234",
        "remit:RA-2024-0474",
        LAST_ID,
    ]
    # Every developed advice reads, so nothing is left for a person.
    assert all(page.unparsed == () for page in pages)
    ids = [record.record_id for page in pages for record in page.records]
    assert len(ids) == len(set(ids)) == DEVELOPED
    assert ids == [row["id"] for row in developed]
    assert (ids[0], ids[-1]) == (FIRST_ID, LAST_ID)
    # Ids the ground-truth manifest knows, as evidence, with no translation step.
    assert set(ids) <= set(group_of)


def test_the_default_page_is_fifty(offline: RemittanceService) -> None:
    page = offline.export_remittances()
    assert (len(page.records), page.examined, page.has_more) == (50, 50, True)
    assert page.next_after_id == "remit:RA-2024-0059"


def test_the_export_resumes_after_any_id_whether_or_not_it_is_on_disk(
    offline: RemittanceService,
) -> None:
    # `remit:RA-2024-0008` is not among the developed advices; the keyset does not need it.
    page = offline.export_remittances(after_id="remit:RA-2024-0008", limit=3)
    assert [record.record_id for record in page.records] == [
        "remit:RA-2024-0010",
        "remit:RA-2024-0011",
        "remit:RA-2024-0012",
    ]
    assert offline.export_remittances(after_id="RA-2024-0008", limit=3) == page
    # An empty or blank cursor is the beginning.
    assert offline.export_remittances(after_id="  ", limit=1).records[0].record_id == FIRST_ID


def test_past_the_last_advice_there_is_an_empty_page_not_an_error(
    offline: RemittanceService,
) -> None:
    page = offline.export_remittances(after_id=LAST_ID)
    assert (page.records, page.unparsed, page.next_after_id, page.has_more) == ((), (), None, False)


def test_the_export_and_the_lookup_give_the_same_record(offline: RemittanceService) -> None:
    page = offline.export_remittances(limit=25)
    for record in page.records:
        assert offline.get_remittance_for_payment(record.reference or "") == record


# -- how much work a call does ---------------------------------------------------------------


def fresh(developed_dir: Path, static_parties: StaticParties) -> tuple[RemittanceService, Counting]:
    settings = RemittanceSettings(data_dir=developed_dir, dsn=NO_DATABASE)
    counting = Counting(ReadOnlyAdvices(settings.remit_dir, settings.fx_path, settings.max_bytes))
    return RemittanceService(settings, parties=static_parties, advices=counting), counting


def test_a_lookup_opens_one_file_and_lists_nothing(
    developed_dir: Path, static_parties: StaticParties
) -> None:
    service, source = fresh(developed_dir, static_parties)
    service.get_remittance_for_payment("PAY-2024-0004")
    service.get_remittance_lines("remit:RA-2024-0006")
    assert source.pdfs == ["PAY-2024-0004", "PAY-2024-0006"]
    assert source.listings == 0
    # Two sterling advices: the rate table was not needed, so it was not read.
    assert source.fx == 0


def test_an_advice_is_parsed_once_however_often_it_is_asked_for(
    developed_dir: Path, static_parties: StaticParties
) -> None:
    service, source = fresh(developed_dir, static_parties)
    first = service.get_remittance_for_payment("PAY-2024-0027")
    for _ in range(3):
        assert service.get_remittance_for_payment("PAY-2024-0027") == first
        service.get_remittance_lines("remit:RA-2024-0027")
    service.export_remittances(after_id="remit:RA-2024-0026", limit=1)
    assert source.pdfs == ["PAY-2024-0027"]
    assert source.fx == 1


def test_a_page_reads_only_the_advices_it_examines(
    developed_dir: Path, static_parties: StaticParties
) -> None:
    service, source = fresh(developed_dir, static_parties)
    page = service.export_remittances(limit=7)
    assert len(source.pdfs) == page.examined == 7
    assert source.listings == 1


def test_each_distinct_payer_name_is_resolved_once(offline: RemittanceService) -> None:
    walk(offline, 200)
    assert len(offline._resolved) == DISTINCT_PAYERS


# -- arguments -------------------------------------------------------------------------------


def test_the_two_argument_readers_agree_about_one_advice() -> None:
    assert parse_payment_ref("PAY-2024-0088") == "PAY-2024-0088"
    assert parse_remittance_id("remit:RA-2024-0088") == "PAY-2024-0088"
    assert parse_remittance_id("RA-2024-12345") == "PAY-2024-12345"
    with pytest.raises(ToolError):
        parse_payment_ref("remit:RA-2024-0088")
    with pytest.raises(ToolError):
        parse_remittance_id("PAY-2024-0088")


# -- with the names from Postgres ------------------------------------------------------------


def test_the_real_server_reads_the_same_names_from_postgres(
    remittance: RemittanceService, offline: RemittanceService
) -> None:
    assert len(remittance.parties.name_index()) == COUNTERPARTIES
    assert remittance.parties.registry().frozen is True
    for payment_ref in ("PAY-2024-0001", "PAY-2024-0002", "PAY-2024-0027", "PAY-2024-0065"):
        assert remittance.get_remittance_for_payment(
            payment_ref
        ) == offline.get_remittance_for_payment(payment_ref)
    page = remittance.export_remittances(limit=120)
    assert page.records == offline.export_remittances(limit=120).records

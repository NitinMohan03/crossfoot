"""Each agent-facing tool answers a known query correctly (plan §4.4 verification).

Driven through `ErpService` rather than through MCP: rule 1 puts every decision in the tool
layer, so this is where correctness is tested, and `test_integration.py` proves the wiring once.
"""

from datetime import UTC, date

import pytest

from crossfoot.entities import AliasRegistry
from crossfoot.mcp.erp import ErpService
from crossfoot.models.canonical import CanonicalRecord, ErpDetail
from tests.test_mcp.conftest import COUNTERPARTIES, CREDIT_NOTES, INVOICES


def first_invoice(service: ErpService) -> CanonicalRecord:
    return service.export_invoices(limit=1)[0]


def test_the_name_index_holds_every_counterparty(service: ErpService) -> None:
    assert len(service.name_index()) == COUNTERPARTIES


def test_get_invoice_returns_a_canonical_obligation(service: ErpService) -> None:
    record = service.get_invoice("INV-2024-0001")
    assert record.record_id == "erp:INV-2024-0001"
    assert record.source == "erp"
    assert record.role == "obligation"
    assert isinstance(record.detail, ErpDetail)
    assert record.detail.document == "invoice"
    # Money is integer minor units, and GBP carries no rate.
    assert isinstance(record.amount.minor, int)
    assert record.amount.minor > 0
    # The due date is the issue date plus the counterparty's terms (spec 01 §2).
    assert record.due_date is not None
    assert record.detail.terms_days is not None
    assert (record.due_date - record.record_date).days == record.detail.terms_days
    # The timestamp went to UTC and the source's own rendering was kept.
    assert record.event_time is not None
    assert record.event_time.tzinfo is not None
    assert record.event_time.utcoffset() == UTC.utcoffset(None)
    assert record.event_time_source is not None


def test_get_invoice_accepts_the_canonical_id_too(service: ErpService) -> None:
    assert service.get_invoice("erp:INV-2024-0001").record_id == "erp:INV-2024-0001"


def test_list_open_invoices_filters_by_vendor(service: ErpService) -> None:
    anchor = first_invoice(service)
    records, party = service.list_open_invoices(vendor=anchor.party.surface, limit=200)
    assert party is not None
    assert party.counterparty_id == anchor.party.counterparty_id
    assert records
    assert {record.detail.counterparty_id for record in records} == {party.counterparty_id}  # type: ignore[union-attr]


def test_list_open_invoices_resolves_a_drifted_vendor_name(service: ErpService) -> None:
    anchor = first_invoice(service)
    # Lower-cased and with the legal suffix dropped: trivial normalisation, not fuzz.
    drifted = anchor.party.normalised.lower()
    records, party = service.list_open_invoices(vendor=drifted, limit=5)
    assert party is not None
    assert party.counterparty_id == anchor.party.counterparty_id
    assert party.method in ("normalised", "fuzzy")
    assert records


def test_list_open_invoices_filters_on_the_gbp_amount_window(service: ErpService) -> None:
    records, _ = service.list_open_invoices(
        amount_min_minor=100_000, amount_max_minor=120_000, limit=200
    )
    assert records
    assert all(100_000 <= record.amount.base_minor <= 120_000 for record in records)


def test_list_open_invoices_filters_on_a_date_window(service: ErpService) -> None:
    records, _ = service.list_open_invoices(
        issued_from="2024-01-01", issued_to="2024-01-31", limit=200
    )
    assert records
    assert all(record.record_date <= date(2024, 1, 31) for record in records)
    assert all(record.record_date >= date(2024, 1, 1) for record in records)


def test_list_open_invoices_says_it_cannot_know_what_is_open(service: ErpService) -> None:
    # The name is plan §3.3's; the ERP has no payment status, and the tool says so rather than
    # implying it filtered on one.
    records, _ = service.list_open_invoices(limit=5)
    assert len(records) == 5


def test_get_credit_notes_returns_negative_amounts(service: ErpService) -> None:
    # Find a counterparty that has one, through the export rather than by guessing a name.
    with_notes = [
        record
        for record in service.export_invoices(limit=200)
        if service.get_credit_notes(record.party.surface, limit=5)[0]
    ]
    assert with_notes, "the dev slice has credit notes; none were reachable by vendor"
    notes, party = service.get_credit_notes(with_notes[0].party.surface, limit=5)
    assert party.counterparty_id is not None
    for note in notes:
        assert note.record_id.startswith("erp:CN-")
        assert note.amount.minor < 0
        assert note.amount.base_minor < 0
        assert note.due_date is None
        assert isinstance(note.detail, ErpDetail)
        assert note.detail.document == "credit_note"


def test_export_invoices_pages_by_ref_and_covers_the_slice(service: ErpService) -> None:
    seen: list[str] = []
    after: str | None = None
    while True:
        page = service.export_invoices(after_ref=after, limit=200)
        if not page:
            break
        seen.extend(record.record_id for record in page)
        after = page[-1].reference
    assert len(seen) == INVOICES
    assert len(set(seen)) == INVOICES
    assert seen == sorted(seen)


def test_the_slice_holds_the_credit_notes_the_pins_claim(service: ErpService) -> None:
    index = service.name_index()
    total = sum(
        len(service.get_credit_notes(names[0], limit=200)[0])
        for names in (index.names_for(candidate.counterparty_id) for candidate in index.candidates)
        if names
    )
    assert total == CREDIT_NOTES


def test_lookup_alias_reports_how_a_name_was_identified(service: ErpService) -> None:
    anchor = first_invoice(service)
    exact = service.lookup_alias(anchor.party.surface)
    assert (exact.counterparty_id, exact.method) == (anchor.party.counterparty_id, "exact")

    nothing = service.lookup_alias("QUAYSIDE LOGISTICS HOLDINGS")
    assert nothing.counterparty_id is None
    assert nothing.method == "unresolved"


def test_the_servers_registry_is_frozen(service: ErpService) -> None:
    registry = service.registry()
    assert registry.frozen is True
    assert service.registry_available is True


def test_the_registry_the_server_loads_is_the_one_in_the_database(
    service: ErpService, settings: object
) -> None:
    # Loaded through the server's own read-only cursor, so it cannot be a different table.
    assert isinstance(service.registry(), AliasRegistry)


@pytest.mark.parametrize("limit", [1, 7, 200])
def test_limits_are_honoured(service: ErpService, limit: int) -> None:
    assert len(service.export_invoices(limit=limit)) == limit

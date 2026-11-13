"""Each tool answers a known query correctly against the committed dev slice.

The API is the real mock from session 2.0, in process; `offline` gives the service a fixed name
index so these run without Postgres, and the last section runs the service as it is deployed,
with names read through the read-only role.
"""

from collections import Counter
from datetime import date

from crossfoot.generate.read import read_bank
from crossfoot.mcp.processor import ProcessorService
from crossfoot.mcp.processor.tools import parse_batch_id, parse_cursor, parse_settlement_id
from tests.test_mcp.conftest import COUNTERPARTIES, DEV_SLICE
from tests.test_mcp.processor.conftest import (
    BASE_URL,
    BATCHES,
    BATCHES_BY_CURRENCY,
    BY_PRODUCT,
    FIRST_ID,
    SETTLEMENTS,
    USD_ID,
    AppTransport,
    api_order,
    detail,
    raw_lines,
)

#: The slice's largest batch, and a foreign one whose payout is not its net total.
BIG_BATCH = "bat_2024-07-17_01"
USD_BATCH = "bat_2024-05-03_03"


# -- get_settlement --------------------------------------------------------------------------


def test_get_settlement_returns_the_line(offline: ProcessorService) -> None:
    record = offline.get_settlement(FIRST_ID)
    assert record.record_id == FIRST_ID
    assert (record.source, record.role) == ("processor", "payment")
    assert (record.amount.minor, record.amount.currency, record.amount.base_minor) == (
        289_375,
        "GBP",
        289_375,
    )
    assert detail(record).kind == "processor"
    assert (detail(record).product, detail(record).fee_minor, detail(record).net_minor) == (
        "card",
        4_109,
        285_266,
    )
    assert detail(record).batch_id == "bat_2024-01-11_01"
    assert record.record_date == date(2024, 1, 11)
    assert (record.party.surface, record.party.counterparty_id, record.party.method) == (
        "KINGSMEAD TRADING LLP",
        28,
        "exact",
    )
    assert record.raw == next(line for line in raw_lines("settlements.jsonl") if FIRST_ID in line)


def test_the_id_is_accepted_with_or_without_its_prefix(
    offline: ProcessorService, transport: AppTransport
) -> None:
    for spelling in (FIRST_ID, FIRST_ID.removeprefix("proc:"), f"  {FIRST_ID}\n"):
        assert offline.get_settlement(spelling).record_id == FIRST_ID
    # Whichever way it was typed, the API was asked for the same record.
    assert {seen.url for seen in transport.requests} == {
        f"{BASE_URL}/v1/settlements/proc%3Astl_c0593907109f"
    }


def test_a_foreign_line_carries_its_rate_and_its_sterling_view(offline: ProcessorService) -> None:
    record = offline.get_settlement(USD_ID)
    assert (record.amount.minor, record.amount.currency) == (3_454, "USD")
    assert (record.amount.base_minor, record.amount.fx_rate_ppm) == (2_591, 1_333_209)
    assert record.reference_surface == "0806"


# -- list_settlements ------------------------------------------------------------------------


def test_list_settlements_by_settlement_date(offline: ProcessorService) -> None:
    day = offline.list_settlements(date_from="2024-06-03", date_to="2024-06-03")
    assert [record.record_id for record in day.records] == [
        "proc:stl_3fca700824f0",
        "proc:stl_e36530009885",
    ]
    assert day.has_more is False
    march = offline.list_settlements(date_from="2024-03-01", date_to="2024-03-31", limit=200)
    assert (len(march.records), march.has_more) == (72, False)
    assert {record.record_date.month for record in march.records} == {3}


def test_list_settlements_is_in_the_apis_order_and_says_when_there_is_more(
    offline: ProcessorService,
) -> None:
    page = offline.list_settlements()
    assert (len(page.records), page.has_more) == (offline.settings.default_limit, True)
    assert page.records[0].record_id == FIRST_ID
    keys = [(record.record_date, record.record_id) for record in page.records]
    assert keys == sorted(keys)
    # A page narrower than the window says so; the same window with room does not.
    assert offline.list_settlements("2024-03-01", "2024-03-31", limit=71).has_more is True
    assert offline.list_settlements("2024-03-01", "2024-03-31", limit=72).has_more is False


def test_open_ended_windows_and_empty_ones(offline: ProcessorService) -> None:
    upto = offline.list_settlements(date_to="2024-01-12")
    assert [record.record_date.isoformat() for record in upto.records] == [
        "2024-01-11",
        "2024-01-12",
    ]
    since = offline.list_settlements(date_from="2025-01-16")
    assert {record.record_date for record in since.records} == {date(2025, 1, 16)}
    nothing = offline.list_settlements(date_from="2023-01-01", date_to="2023-01-31")
    assert (nothing.records, nothing.has_more) == ((), False)


# -- get_batch -------------------------------------------------------------------------------


def test_get_batch_returns_the_batch_and_every_line_in_it(offline: ProcessorService) -> None:
    found = offline.get_batch(BIG_BATCH)
    batch = found.batch
    assert (batch.id, batch.count, batch.currency) == (BIG_BATCH, 8, "GBP")
    assert (batch.closed_on, batch.paid_out_on) == (date(2024, 7, 17), date(2024, 7, 18))
    assert batch.total_net_minor == batch.payout_base_minor == 2_754_479
    assert (len(found.records), found.total_lines) == (8, 8)
    assert {detail(record).batch_id for record in found.records} == {BIG_BATCH}
    # The arithmetic the batch claims, done here in integers on what came back.
    assert sum(detail(record).net_minor for record in found.records) == 2_754_479
    assert {record.record_date for record in found.records} == {batch.closed_on}


def test_limit_bounds_the_lines_returned_not_the_lines_counted(
    offline: ProcessorService,
) -> None:
    found = offline.get_batch(BIG_BATCH, limit=3)
    assert [record.record_id for record in found.records] == [
        "proc:stl_24663614e230",
        "proc:stl_8302bd5e99ce",
        "proc:stl_83d73fa1fce4",
    ]
    assert found.total_lines == 8


def test_a_foreign_batch_pays_out_in_sterling_at_another_rate(
    offline: ProcessorService,
) -> None:
    found = offline.get_batch(USD_BATCH)
    batch = found.batch
    assert (batch.currency, batch.count) == ("USD", 2)
    # The lines add up to the batch in dollars; what reached the bank is pounds, converted on
    # the payout date, and is not the sum of the lines' own sterling views.
    assert sum(detail(record).net_minor for record in found.records) == batch.total_net_minor
    assert (batch.total_net_minor, batch.payout_base_minor) == (1_024_921, 798_183)
    assert {record.amount.currency for record in found.records} == {"USD"}
    assert sum(record.amount.base_minor for record in found.records) != batch.payout_base_minor
    # Three batches closed that day, two of them in other currencies: only this one's lines.
    assert {detail(record).batch_id for record in found.records} == {USD_BATCH}


def test_every_bank_payout_line_opens_onto_its_batch(offline: ProcessorService) -> None:
    # The cross-source question this tool exists for. The bank writes one `TFR` line per batch
    # and names the batch in upper case; asked with exactly that text, the processor says what
    # was inside, and its payout is that bank line to the penny and to the day.
    payouts = [line for line in read_bank(DEV_SLICE / "bank") if line.txn_type == "TFR"]
    assert len(payouts) == BATCHES
    lines = 0
    for payout in payouts:
        reference = payout.description.split(" ")[-1]
        assert reference.startswith("BAT_")
        found = offline.get_batch(reference, limit=1)
        assert found.batch.id == reference.lower()
        assert found.batch.payout_base_minor == payout.paid_in_minor
        assert found.batch.paid_out_on == payout.value_date
        lines += found.total_lines
    # Every settlement line is in exactly one batch.
    assert lines == SETTLEMENTS


# -- the bulk reads --------------------------------------------------------------------------


def test_export_settlements_pages_through_everything_exactly_once(
    offline: ProcessorService, manifest_proc_ids: set[str]
) -> None:
    pages = []
    cursor: str | None = None
    while True:
        page = offline.export_settlements(cursor=cursor, limit=200)
        pages.append(page)
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert [len(page.records) for page in pages] == [200, 200, 152]
    assert pages[-1].next_cursor is None
    records = [record for page in pages for record in page.records]
    ids = [record.record_id for record in records]
    assert len(ids) == len(set(ids)) == SETTLEMENTS
    assert set(ids) == manifest_proc_ids
    # The API's order, and the committed bytes.
    assert [record.raw for record in records] == api_order("settlements.jsonl", "settled_on")
    assert Counter(detail(record).product for record in records) == BY_PRODUCT


def test_export_settlements_defaults_to_the_first_page(offline: ProcessorService) -> None:
    page = offline.export_settlements()
    assert (len(page.records), page.has_more) == (50, True)
    assert page.next_cursor
    assert offline.export_settlements(cursor="", limit=1).records[0].record_id == FIRST_ID


def test_export_batches_pages_through_every_batch(offline: ProcessorService) -> None:
    pages = []
    cursor: str | None = None
    while True:
        page = offline.export_batches(cursor=cursor, limit=100)
        pages.append(page)
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert [len(page.batches) for page in pages] == [100, 100, 68]
    batches = [batch for page in pages for batch in page.batches]
    assert len({batch.id for batch in batches}) == BATCHES
    assert Counter(batch.currency for batch in batches) == BATCHES_BY_CURRENCY
    assert sum(batch.count for batch in batches) == SETTLEMENTS
    keys = [(batch.closed_on, batch.id) for batch in batches]
    assert keys == sorted(keys)
    # A sterling batch pays out what it netted; a foreign one is converted, and the payout
    # comes on a later day than the close.
    for batch in batches:
        assert batch.paid_out_on > batch.closed_on
        if batch.currency == "GBP":
            assert batch.payout_base_minor == batch.total_net_minor


# -- the arguments, on their own -------------------------------------------------------------


def test_ids_and_cursors_are_normalised_one_way() -> None:
    assert parse_settlement_id("stl_c0593907109f") == FIRST_ID
    assert parse_batch_id(" BAT_2024-07-17_01 ") == BIG_BATCH
    assert parse_cursor(None) is None
    assert parse_cursor("   ") is None
    assert parse_cursor(" abc ") == "abc"


def test_a_payer_name_is_resolved_once_however_often_it_appears(
    offline: ProcessorService,
) -> None:
    calls: list[str] = []
    index = offline.parties.name_index

    def counting() -> object:
        calls.append("index")
        return index()

    offline.parties.name_index = counting  # type: ignore[method-assign,assignment]
    cursor: str | None = None
    while True:
        page = offline.export_settlements(cursor=cursor, limit=200)
        if not page.has_more:
            break
        cursor = page.next_cursor
    # 552 lines, 92 distinct payer names, 92 resolutions.
    assert len(calls) == 92


# -- as deployed: names from Postgres --------------------------------------------------------


def test_the_names_come_from_the_erp_through_the_read_only_role(
    processor: ProcessorService,
) -> None:
    record = processor.get_settlement(FIRST_ID)
    assert (record.party.counterparty_id, record.party.method) == (28, "exact")
    assert len(processor.parties.name_index()) == COUNTERPARTIES
    # The registry is constructed frozen: a server never learns an alias.
    assert processor.parties.registry().frozen is True


def test_postgres_and_the_fixed_index_resolve_every_line_alike(
    processor: ProcessorService, offline: ProcessorService
) -> None:
    live = processor.export_settlements(limit=200).records
    fixed = offline.export_settlements(limit=200).records
    assert [record.party for record in live] == [record.party for record in fixed]

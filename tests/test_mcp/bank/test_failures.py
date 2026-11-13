"""Bad input and a broken source degrade, they do not crash (plan §4.4 verification).

Both levels are checked, as for `mcp-erp`: the tool layer raises a coded `ToolError`, and the
server returns that code in the envelope instead of failing the call. What is specific to this
source is what "broken" means -- a row that will not parse, bytes in the wrong encoding, a month
or a directory that is not there -- and how far the damage is allowed to spread: one bad month
fails the calls that touch it and no others.
"""

import asyncio
from typing import Any

import pytest
from mcp import Client

from crossfoot.mcp.bank import BankService, BankSettings, build_server
from crossfoot.mcp.common.errors import (
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    MALFORMED_SOURCE,
    NOT_FOUND,
    UNAVAILABLE,
    ToolError,
)
from tests.test_mcp.bank.conftest import BANK_DIR, NO_DATABASE, Builder
from tests.test_mcp.conftest import DEV_SLICE
from tests.test_mcp.test_failures import call, error_of

JUNE = "2024-06.csv"


def june_with(old: bytes, new: bytes) -> bytes:
    original = (BANK_DIR / JUNE).read_bytes()
    assert old in original
    return original.replace(old, new, 1)


# -- arguments -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line_id",
    [
        "INV-2024-0001",
        "bank:2024-03-01",
        "bank:2024-03-01:1",
        "bank:2024-3-1:000001",
        "bank:01/03/2024:000001",
        "erp:2024-03-01:000001",
        "bank:2024-13-01:000001",
        "bank:2024-02-30:000001",
        "   ",
        "",
    ],
)
def test_an_id_that_is_not_a_bank_line_id_is_an_invalid_argument(
    offline: BankService, line_id: str
) -> None:
    with pytest.raises(ToolError) as caught:
        offline.get_bank_line(line_id)
    assert caught.value.code == INVALID_ARGUMENT


@pytest.mark.parametrize(
    "line_id",
    [
        "bank:2024-03-01:000999",  # a row the month does not have
        "bank:2024-03-01:000000",  # rows are numbered from one
        "bank:2024-03-02:000001",  # row 1 exists, but its value date is the 1st
        "bank:2023-12-29:000001",  # a month the export has no file for
    ],
)
def test_a_well_formed_id_that_names_no_line_is_not_found(
    offline: BankService, line_id: str
) -> None:
    with pytest.raises(ToolError) as caught:
        offline.get_bank_line(line_id)
    assert caught.value.code == NOT_FOUND


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount_min_minor": 500, "amount_max_minor": 100},
        {"date_from": "2024-06-01", "date_to": "2024-01-01"},
    ],
)
def test_a_backwards_range_is_an_invalid_argument(
    offline: BankService, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as caught:
        offline.find_bank_lines(**kwargs)
    assert caught.value.code == INVALID_ARGUMENT


@pytest.mark.parametrize("value", ["2024-13-01", "01/03/2024", "yesterday", "2024-02-30"])
def test_an_unparseable_date_is_an_invalid_argument(offline: BankService, value: str) -> None:
    # The CSV writes DD/MM/YYYY; the tool takes ISO dates only, and says so rather than guess
    # which of 01/03 and 03/01 was meant.
    with pytest.raises(ToolError) as caught:
        offline.find_bank_lines(date_from=value)
    assert caught.value.code == INVALID_ARGUMENT


def test_a_limit_below_one_is_an_invalid_argument(offline: BankService) -> None:
    with pytest.raises(ToolError) as caught:
        offline.export_bank_lines(limit=0)
    assert caught.value.code == INVALID_ARGUMENT


def test_a_limit_above_the_maximum_says_what_the_maximum_is(offline: BankService) -> None:
    with pytest.raises(ToolError) as caught:
        offline.find_bank_lines(limit=10_000)
    assert caught.value.code == LIMIT_EXCEEDED
    assert caught.value.extra["max_limit"] == offline.settings.max_limit


def test_a_malformed_after_id_is_an_invalid_argument(offline: BankService) -> None:
    with pytest.raises(ToolError) as caught:
        offline.export_bank_lines(after_id="INV-2024-0001")
    assert caught.value.code == INVALID_ARGUMENT
    assert "after_id" in caught.value.message


def test_an_empty_result_is_an_empty_list_not_an_error(offline: BankService) -> None:
    assert offline.find_bank_lines(date_from="2023-01-01", date_to="2023-01-02") == ([], 0)
    assert offline.find_bank_lines(amount_min_minor=10**12) == ([], 0)


# -- a broken source -------------------------------------------------------------------------


def test_a_malformed_row_fails_the_month_it_is_in_and_names_the_row(broken: Builder) -> None:
    service = broken(**{JUNE: june_with(b",FPI,KINGSMEAD", b",CHQ,KINGSMEAD")})
    with pytest.raises(ToolError) as caught:
        service.get_bank_line("bank:2024-06-03:000002")
    assert caught.value.code == MALFORMED_SOURCE
    assert caught.value.extra == {"file": JUNE, "row": 1}
    assert "unknown transaction type 'CHQ'" in caught.value.message


def test_a_malformed_month_does_not_take_the_other_months_with_it(broken: Builder) -> None:
    service = broken(**{JUNE: june_with(b",FPI,KINGSMEAD", b",CHQ,KINGSMEAD")})
    # A line in March, a window that ends in May, and the export's first pages: all fine.
    assert service.get_bank_line("bank:2024-03-01:000001").amount.minor == 249_230
    assert service.find_bank_lines(date_to="2024-05-31", limit=1)[1] == 423
    assert len(service.export_bank_lines(limit=200)) == 200
    assert service.find_bank_lines(date_from="2024-07-01", limit=1)[1] > 0
    # Anything that has to read June is refused whole. A window with a hole in it would look
    # like an answer.
    for attempt in (
        lambda: service.find_bank_lines(date_from="2024-05-30", date_to="2024-06-04"),
        lambda: service.find_bank_lines(amount_min_minor=1),
        lambda: service.export_bank_lines(after_id="bank:2024-05-31:000123", limit=5),
    ):
        with pytest.raises(ToolError) as caught:
            attempt()
        assert caught.value.code == MALFORMED_SOURCE
    # And the service is still good for the next call.
    assert service.get_bank_line("bank:2024-03-01:000001").record_id == "bank:2024-03-01:000001"


def test_a_row_dropped_from_a_statement_is_caught_by_the_balance(broken: Builder) -> None:
    lines = (BANK_DIR / JUNE).read_bytes().split(b"\r\n")
    del lines[5]  # the header is line 0, so this is data row 5
    service = broken(**{JUNE: b"\r\n".join(lines)})
    with pytest.raises(ToolError) as caught:
        service.get_bank_line("bank:2024-06-03:000001")
    assert caught.value.code == MALFORMED_SOURCE
    assert caught.value.extra == {"file": JUNE, "row": 5}


def test_a_statement_in_the_wrong_encoding_is_a_coded_error(broken: Builder) -> None:
    service = broken(**{JUNE: june_with(b"BRAMBLE DESIGN LTD", b"BRAMBLE D\xa3SIGN LTD")})
    with pytest.raises(ToolError) as caught:
        service.find_bank_lines(date_from="2024-06-01", date_to="2024-06-30")
    assert caught.value.code == MALFORMED_SOURCE
    assert "not UTF-8" in caught.value.message
    assert caught.value.extra == {"file": JUNE}


def test_an_empty_statement_file_is_malformed_not_empty(broken: Builder) -> None:
    service = broken(**{JUNE: b""})
    with pytest.raises(ToolError) as caught:
        service.find_bank_lines(date_from="2024-06-01", date_to="2024-06-30")
    assert caught.value.code == MALFORMED_SOURCE


def test_a_missing_month_is_a_gap_the_other_months_work_around(broken: Builder) -> None:
    service = broken(**{JUNE: None})
    with pytest.raises(ToolError) as caught:
        service.get_bank_line("bank:2024-06-03:000001")
    assert caught.value.code == NOT_FOUND
    assert service.find_bank_lines(limit=1)[1] == 1210 - 110


def test_a_missing_export_is_unavailable_from_every_tool(static_parties: Any) -> None:
    service = BankService(
        BankSettings(data_dir=DEV_SLICE / "no-such-dataset", dsn=NO_DATABASE),
        parties=static_parties,
    )
    for attempt in (
        lambda: service.get_bank_line("bank:2024-03-01:000001"),
        lambda: service.find_bank_lines(limit=1),
        lambda: service.export_bank_lines(limit=1),
    ):
        with pytest.raises(ToolError) as caught:
            attempt()
        assert caught.value.code == UNAVAILABLE
        assert "just seed" in caught.value.message


def test_an_export_without_the_sidecar_still_answers_with_no_timestamps(broken: Builder) -> None:
    # A real bank export has no timestamp column at all (spec 01 §3.2). Its absence is not an
    # error, and it is not papered over with midnight either.
    service = broken(**{"posted_at.jsonl": None})
    line = service.get_bank_line("bank:2024-04-02:000003")
    assert line.event_time is None
    assert line.event_time_source is None
    assert line.record_date.isoformat() == "2024-04-02"


def test_a_sidecar_timestamp_with_no_offset_is_malformed_not_assumed_local(
    broken: Builder,
) -> None:
    sidecar = (BANK_DIR / "posted_at.jsonl").read_bytes()
    naive = sidecar.replace(b"2024-04-02T00:02:00+01:00", b"2024-04-02T00:02:00", 1)
    assert naive != sidecar
    service = broken(**{"posted_at.jsonl": naive})
    with pytest.raises(ToolError) as caught:
        service.get_bank_line("bank:2024-03-01:000001")
    assert caught.value.code == MALFORMED_SOURCE
    assert "no UTC offset" in caught.value.message


# -- the other source: names from Postgres ---------------------------------------------------


def test_an_unreachable_database_is_unavailable_not_an_unresolved_party() -> None:
    # The lines are on disk and readable. They are still not served: a payment that reads as
    # coming from nobody, because the name list was down, would be stored as exactly that.
    service = BankService(BankSettings(data_dir=DEV_SLICE, dsn=NO_DATABASE, connect_timeout=2))
    try:
        with pytest.raises(ToolError) as caught:
            service.get_bank_line("bank:2024-03-01:000001")
        assert caught.value.code == UNAVAILABLE
        # The DSN carries a password and must not appear in what the agent is told.
        assert "crossfoot_ro" not in caught.value.message
        # A processor payout names no counterparty, so it needs no name list.
        assert service.get_bank_line("bank:2024-01-12:000002").party.method == "unresolved"
    finally:
        service.close()


def test_a_schema_with_no_counterparties_is_unavailable_and_says_what_to_run(
    bank_settings: BankSettings,
) -> None:
    absent = BankSettings(
        data_dir=bank_settings.data_dir, dsn=bank_settings.dsn, erp_schema="erp_does_not_exist"
    )
    service = BankService(absent)
    try:
        with pytest.raises(ToolError) as caught:
            service.get_bank_line("bank:2024-03-01:000001")
        assert caught.value.code == UNAVAILABLE
        assert "db-load" in caught.value.message
    finally:
        service.close()


def test_a_schema_name_that_is_not_an_identifier_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="not a usable schema name"):
        BankSettings(dsn="postgresql://x/y", erp_schema="erp; DROP SCHEMA public")


# -- the server returns that code, and keeps working -----------------------------------------


def test_the_server_returns_the_envelope_instead_of_failing_the_call(
    offline: BankService,
) -> None:
    server = build_server(offline.settings, offline)
    assert error_of(call(server, "get_bank_line", {"id": "bank:2024-03-01:000999"}))[0] == NOT_FOUND
    assert error_of(call(server, "get_bank_line", {"id": "INV-2024-0001"}))[0] == INVALID_ARGUMENT
    assert error_of(call(server, "find_bank_lines", {"date_from": "nope"}))[0] == INVALID_ARGUMENT
    assert error_of(call(server, "export_bank_lines", {"limit": 10_000}))[0] == LIMIT_EXCEEDED


def test_a_malformed_source_crosses_the_server_as_a_code_with_its_file_and_row(
    broken: Builder,
) -> None:
    service = broken(**{JUNE: june_with(b",FPI,KINGSMEAD", b",CHQ,KINGSMEAD")})
    payload = call(build_server(service.settings, service), "find_bank_lines", {})
    assert error_of(payload)[0] == MALFORMED_SOURCE
    assert (payload["error"]["file"], payload["error"]["row"]) == (JUNE, 1)


def test_an_unavailable_source_crosses_the_server_as_a_code(static_parties: Any) -> None:
    settings = BankSettings(data_dir=DEV_SLICE / "no-such-dataset", dsn=NO_DATABASE)
    server = build_server(settings, BankService(settings, parties=static_parties))
    assert error_of(call(server, "export_bank_lines", {}))[0] == UNAVAILABLE


def test_an_empty_result_crosses_the_server_as_an_empty_list(offline: BankService) -> None:
    payload = call(
        build_server(offline.settings, offline),
        "find_bank_lines",
        {"date_from": "2023-01-01", "date_to": "2023-01-02"},
    )
    assert payload == {"records": [], "count": 0, "total_matches": 0}


def test_a_failed_call_does_not_poison_the_session(broken: Builder) -> None:
    service = broken(**{JUNE: june_with(b",FPI,KINGSMEAD", b",CHQ,KINGSMEAD")})
    server = build_server(service.settings, service)

    async def sequence() -> list[dict[str, Any]]:
        async with Client(server) as client:
            results = [
                await client.call_tool("get_bank_line", {"id": "bank:2024-06-03:000001"}),
                await client.call_tool("get_bank_line", {"id": "nonsense"}),
                await client.call_tool("get_bank_line", {"id": "bank:2024-03-01:000001"}),
            ]
            assert all(result.is_error is False for result in results)
            return [dict(result.structured_content or {}) for result in results]

    malformed, invalid, good = asyncio.run(sequence())
    assert error_of(malformed)[0] == MALFORMED_SOURCE
    assert error_of(invalid)[0] == INVALID_ARGUMENT
    assert good["record_id"] == "bank:2024-03-01:000001"

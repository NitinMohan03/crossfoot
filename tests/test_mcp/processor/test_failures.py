"""Bad input and a broken source degrade, they do not crash (plan §4.4 verification).

Both levels are checked, as for the other servers: the tool layer raises a coded `ToolError`,
and the server returns that code in the envelope instead of failing the call. What is specific
to this source is what "broken" means when the source is a service -- a credential it refuses, a
rate limit, no answer, an answer that is not what was documented, an answer that contradicts
itself -- and that none of them takes the session with it.
"""

import asyncio
import json
from collections.abc import Callable
from functools import partial
from typing import Any

import pytest
from mcp import Client

from crossfoot.mcp.common.errors import (
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    MALFORMED_SOURCE,
    NOT_FOUND,
    UNAVAILABLE,
    ToolError,
)
from crossfoot.mcp.common.parties import StaticParties
from crossfoot.mcp.processor import ProcessorService, ProcessorSettings, build_server
from crossfoot.mcp.processor import tools as tools_module
from crossfoot.mcp.processor.http import SourceDown
from tests.test_mcp.processor.conftest import (
    FIRST_ID,
    NO_DATABASE,
    TOKEN,
    AppTransport,
    FakeClock,
    ScriptedTransport,
    api_error,
    app_client,
    client_settings,
    free_port,
    raw_lines,
    reply,
    service_over,
)
from tests.test_mcp.test_failures import call, error_of

FIRST_LINE = next(line for line in raw_lines("settlements.jsonl") if FIRST_ID in line)
FIRST_BATCH = raw_lines("batches.jsonl")[0]
BATCH_ID = "bat_2024-01-11_01"

Attempt = Callable[[ProcessorService], Any]

#: One call per tool, each with arguments that are fine: what fails is the source.
EVERY_TOOL: tuple[Attempt, ...] = (
    lambda service: service.get_settlement(FIRST_ID),
    lambda service: service.list_settlements(date_from="2024-01-11"),
    lambda service: service.get_batch(BATCH_ID),
    lambda service: service.export_settlements(limit=5),
    lambda service: service.export_batches(limit=5),
)


def refused(call: Callable[[], Any], code: str) -> ToolError:
    with pytest.raises(ToolError) as caught:
        call()
    assert caught.value.code == code, caught.value.message
    assert TOKEN not in json.dumps(caught.value.payload())
    return caught.value


def page(*records: str) -> dict[str, Any]:
    return {"data": [json.loads(raw) for raw in records], "next_cursor": None, "has_more": False}


def edited(raw: str, **changes: Any) -> str:
    record = json.loads(raw)
    record.update(changes)
    return json.dumps(record)


# -- arguments: answered here, with no request sent -------------------------------------------


@pytest.mark.parametrize(
    "settlement_id",
    [
        "",
        "   ",
        "INV-2024-0001",
        "proc:stl_123",
        "proc:stl_c0593907109g",
        "proc:STL_C0593907109F",
        "bank:stl_c0593907109f",
        "stl_c0593907109f/../../batches",
        "proc:stl_c0593907109f?limit=500",
        "bat_2024-01-11_01",
    ],
)
def test_an_id_that_is_not_a_settlement_id_is_an_invalid_argument(
    offline: ProcessorService, transport: AppTransport, settlement_id: str
) -> None:
    refused(lambda: offline.get_settlement(settlement_id), INVALID_ARGUMENT)
    # Nothing that is not an id is ever put in a URL.
    assert transport.requests == []


@pytest.mark.parametrize(
    "batch_id",
    ["", "bat_1", "batch", "bat_2024-01-11", "bat_2024-01-11_1", "proc:stl_c0593907109f", "../x"],
)
def test_an_id_that_is_not_a_batch_id_is_an_invalid_argument(
    offline: ProcessorService, transport: AppTransport, batch_id: str
) -> None:
    refused(lambda: offline.get_batch(batch_id), INVALID_ARGUMENT)
    assert transport.requests == []


@pytest.mark.parametrize("value", ["2024-13-01", "01/03/2024", "yesterday", "2024-02-30"])
def test_an_unparseable_date_is_an_invalid_argument(
    offline: ProcessorService, transport: AppTransport, value: str
) -> None:
    refused(lambda: offline.list_settlements(date_from=value), INVALID_ARGUMENT)
    refused(lambda: offline.list_settlements(date_to=value), INVALID_ARGUMENT)
    assert transport.requests == []


def test_a_backwards_window_is_an_invalid_argument(
    offline: ProcessorService, transport: AppTransport
) -> None:
    refused(lambda: offline.list_settlements("2024-06-01", "2024-01-01"), INVALID_ARGUMENT)
    assert transport.requests == []


def test_a_limit_below_one_is_an_invalid_argument(offline: ProcessorService) -> None:
    for attempt in (
        lambda: offline.list_settlements(limit=0),
        lambda: offline.get_batch(BATCH_ID, limit=0),
        lambda: offline.export_settlements(limit=-5),
        lambda: offline.export_batches(limit=0),
    ):
        refused(attempt, INVALID_ARGUMENT)


def test_a_limit_above_the_maximum_says_what_the_maximum_is(
    offline: ProcessorService, transport: AppTransport
) -> None:
    for attempt in (
        lambda: offline.list_settlements(limit=10_000),
        lambda: offline.get_batch(BATCH_ID, limit=201),
        lambda: offline.export_settlements(limit=201),
        lambda: offline.export_batches(limit=201),
    ):
        error = refused(attempt, LIMIT_EXCEEDED)
        assert error.extra == {"max_limit": offline.settings.max_limit}
    assert transport.requests == []


def test_a_cursor_the_api_did_not_issue_is_an_invalid_argument(
    offline: ProcessorService,
) -> None:
    assert (
        "cursor"
        in refused(
            lambda: offline.export_settlements(cursor="not-a-cursor"), INVALID_ARGUMENT
        ).message
    )
    # A real cursor, from the other list.
    theirs = offline.export_batches(limit=1).next_cursor
    refused(lambda: offline.export_settlements(cursor=theirs), INVALID_ARGUMENT)
    refused(lambda: offline.export_batches(cursor="x" * 513), INVALID_ARGUMENT)


def test_an_unknown_id_is_not_found(offline: ProcessorService) -> None:
    refused(lambda: offline.get_settlement("proc:stl_000000000000"), NOT_FOUND)
    refused(lambda: offline.get_batch("bat_2030-01-01_01"), NOT_FOUND)


def test_an_empty_result_is_an_empty_page_not_an_error(offline: ProcessorService) -> None:
    nothing = offline.list_settlements(date_from="2023-01-01", date_to="2023-01-02")
    assert (nothing.records, nothing.has_more) == ((), False)


# -- the source refuses, stalls, or is not there ----------------------------------------------


def test_a_bad_token_is_unavailable_from_every_tool(
    transport: AppTransport, static_parties: StaticParties
) -> None:
    service = service_over(transport, static_parties, token="wrong-token")
    for attempt in EVERY_TOOL:
        error = refused(partial(attempt, service), UNAVAILABLE)
        assert error.extra == {
            "upstream_status": 401,
            "upstream_code": "invalid_token",
            "retryable": False,
        }
        assert "wrong-token" not in json.dumps(error.payload())
    # One request per call: a refused credential is not retried.
    assert len(transport.requests) == len(EVERY_TOOL)


def test_a_rate_limit_is_waited_out_and_the_tool_still_answers(
    static_parties: StaticParties,
) -> None:
    clock = FakeClock()
    with app_client(clock, rate_per_second=1, burst=1) as limited:
        transport = AppTransport(limited)
        service = service_over(transport, static_parties, clock)
        # get_batch makes two requests against a bucket that holds one.
        found = service.get_batch(BATCH_ID)
        assert found.total_lines == 1
        assert clock.sleeps == [1]
        assert len(transport.requests) == 3


def test_a_rate_limit_that_does_not_lift_is_unavailable_with_the_wait(
    static_parties: StaticParties,
) -> None:
    transport = ScriptedTransport(api_error(429, "rate_limited", retry_after="2"))
    clock = FakeClock()
    service = service_over(transport, static_parties, clock)
    for index, attempt in enumerate(EVERY_TOOL, start=1):
        error = refused(partial(attempt, service), UNAVAILABLE)
        assert error.extra == {
            "upstream_status": 429,
            "retry_after": 2,
            "attempts": 3,
            "retryable": True,
        }
        # Bounded: three requests and two waits per call, every time.
        assert len(transport.requests) == 3 * index
        assert clock.sleeps == [2, 2] * index


def test_a_source_that_does_not_answer_is_unavailable_from_every_tool(
    static_parties: StaticParties,
) -> None:
    transport = ScriptedTransport(SourceDown("timed out"))
    service = service_over(transport, static_parties)
    for attempt in EVERY_TOOL:
        error = refused(partial(attempt, service), UNAVAILABLE)
        assert error.extra == {"retryable": True}
    assert len(transport.requests) == len(EVERY_TOOL)


def test_a_server_that_is_down_is_unavailable_over_a_real_socket(
    static_parties: StaticParties,
) -> None:
    settings = client_settings(base_url=f"http://127.0.0.1:{free_port()}")
    service = ProcessorService(settings, parties=static_parties)
    error = refused(lambda: service.get_settlement(FIRST_ID), UNAVAILABLE)
    assert "did not answer" in error.message
    assert error.extra == {"retryable": True}


def test_a_500_is_unavailable_from_every_tool(static_parties: StaticParties) -> None:
    service = service_over(ScriptedTransport(reply(503, "<html>")), static_parties)
    for attempt in EVERY_TOOL:
        error = refused(partial(attempt, service), UNAVAILABLE)
        assert error.extra == {"upstream_status": 503, "retryable": True}


# -- the source answers, and the answer is wrong -----------------------------------------------


@pytest.mark.parametrize(
    "body",
    [b"", b"<html>502</html>", b"{", b"\xff\xfe\x00", b"[]", b'{"error":"nope"}', b"null"],
)
def test_a_body_that_is_not_what_was_documented_is_malformed_from_every_tool(
    static_parties: StaticParties, body: bytes
) -> None:
    service = service_over(ScriptedTransport(reply(200, body)), static_parties)
    for attempt in EVERY_TOOL:
        refused(partial(attempt, service), MALFORMED_SOURCE)


def test_one_bad_record_refuses_the_whole_page_and_names_it(
    static_parties: StaticParties,
) -> None:
    good = raw_lines("settlements.jsonl")[:3]
    bad = edited(FIRST_LINE, gross_minor=289375.5)
    service = service_over(ScriptedTransport(reply(200, page(*good, bad))), static_parties)
    for attempt in (
        lambda: service.export_settlements(limit=10),
        lambda: service.list_settlements(),
    ):
        error = refused(attempt, MALFORMED_SOURCE)
        assert "gross_minor" in error.message
        assert error.extra == {"record": FIRST_ID}


def test_a_line_whose_arithmetic_is_off_is_malformed_through_the_tool(
    static_parties: StaticParties,
) -> None:
    body = edited(FIRST_LINE, fee_minor=1)
    service = service_over(ScriptedTransport(reply(200, body)), static_parties)
    error = refused(lambda: service.get_settlement(FIRST_ID), MALFORMED_SOURCE)
    assert "does not equal gross" in error.message
    assert error.extra == {"record": FIRST_ID}


def test_being_sent_a_different_record_than_the_one_asked_for_is_malformed(
    static_parties: StaticParties,
) -> None:
    other = raw_lines("settlements.jsonl")[0]
    assert FIRST_ID not in other
    service = service_over(ScriptedTransport(reply(200, other)), static_parties)
    error = refused(lambda: service.get_settlement(FIRST_ID), MALFORMED_SOURCE)
    assert error.extra == {"record": FIRST_ID}

    wrong_batch = edited(FIRST_BATCH, id="bat_2024-01-12_01")
    service = service_over(ScriptedTransport(reply(200, wrong_batch)), static_parties)
    refused(lambda: service.get_batch(BATCH_ID), MALFORMED_SOURCE)


def test_a_line_outside_the_window_it_was_asked_for_is_malformed(
    static_parties: StaticParties,
) -> None:
    service = service_over(ScriptedTransport(reply(200, page(FIRST_LINE))), static_parties)
    error = refused(
        lambda: service.list_settlements(date_from="2024-02-01", date_to="2024-02-29"),
        MALFORMED_SOURCE,
    )
    assert "2024-01-11" in error.message
    # The same body is fine for a window that does contain it.
    assert len(service.list_settlements("2024-01-01", "2024-01-31").records) == 1


@pytest.mark.parametrize(
    ("batch_changes", "line_changes", "expected"),
    [
        ({"count": 2}, {}, "says it holds 2 lines and 1 carry its id"),
        ({"total_net_minor": 285_267}, {}, "do not add up"),
        ({"currency": "USD"}, {}, "a USD batch holding a line in another currency"),
        ({}, {"batch_id": "bat_2024-01-11_02"}, "says it holds 1 lines and 0 carry its id"),
    ],
)
def test_a_batch_that_contradicts_its_own_lines_is_malformed(
    static_parties: StaticParties,
    batch_changes: dict[str, Any],
    line_changes: dict[str, Any],
    expected: str,
) -> None:
    transport = ScriptedTransport(
        reply(200, edited(FIRST_BATCH, **batch_changes)),
        reply(200, page(edited(FIRST_LINE, **line_changes))),
    )
    error = refused(
        lambda: service_over(transport, static_parties).get_batch(BATCH_ID), MALFORMED_SOURCE
    )
    assert expected in error.message
    assert error.extra == {"record": BATCH_ID}


def test_get_batch_stops_reading_a_day_that_never_ends(
    static_parties: StaticParties, monkeypatch: pytest.MonkeyPatch
) -> None:
    endless = {"data": [json.loads(FIRST_LINE)], "next_cursor": "more", "has_more": True}
    transport = ScriptedTransport(reply(200, FIRST_BATCH), reply(200, endless))
    monkeypatch.setattr(tools_module, "MAX_BATCH_PAGES", 4)
    refused(lambda: service_over(transport, static_parties).get_batch(BATCH_ID), LIMIT_EXCEEDED)
    # The batch, then four pages, then no more.
    assert len(transport.requests) == 5


# -- the other source: names from Postgres ----------------------------------------------------


def test_an_unreachable_database_is_unavailable_not_an_unresolved_party(
    transport: AppTransport,
) -> None:
    # The API answered. The lines are still not served: a payment that reads as coming from
    # nobody, because the name list was down, would be stored as exactly that.
    service = service_over(transport, None, connect_timeout=2)
    try:
        for attempt in EVERY_TOOL[:4]:
            error = refused(partial(attempt, service), UNAVAILABLE)
            # The DSN carries a password and must not appear in what the agent is told.
            assert "crossfoot_ro" not in error.message
        # A batch list names no payer, so it needs no name list.
        assert len(service.export_batches(limit=5).batches) == 5
    finally:
        service.close()


def test_a_schema_with_no_counterparties_is_unavailable_and_says_what_to_run(
    processor_settings: ProcessorSettings, transport: AppTransport
) -> None:
    service = service_over(
        transport, None, dsn=processor_settings.dsn, erp_schema="erp_does_not_exist"
    )
    try:
        error = refused(lambda: service.get_settlement(FIRST_ID), UNAVAILABLE)
        assert "db-load" in error.message
    finally:
        service.close()


# -- the server returns that code, and keeps working -------------------------------------------


def test_the_server_returns_the_envelope_instead_of_failing_the_call(
    offline: ProcessorService,
) -> None:
    server = build_server(offline.settings, offline)
    missing = {"id": "proc:stl_000000000000"}
    assert error_of(call(server, "get_settlement", missing))[0] == NOT_FOUND
    assert error_of(call(server, "get_settlement", {"id": "INV-1"}))[0] == INVALID_ARGUMENT
    assert error_of(call(server, "get_batch", {"id": "bat_2030-01-01_01"}))[0] == NOT_FOUND
    assert error_of(call(server, "list_settlements", {"date_from": "x"}))[0] == INVALID_ARGUMENT
    assert error_of(call(server, "export_settlements", {"limit": 10_000}))[0] == LIMIT_EXCEEDED
    assert error_of(call(server, "export_batches", {"cursor": "zzz"}))[0] == INVALID_ARGUMENT


def test_a_rate_limit_crosses_the_server_with_the_wait_the_agent_needs(
    static_parties: StaticParties,
) -> None:
    transport = ScriptedTransport(api_error(429, "rate_limited", retry_after="60"))
    service = service_over(transport, static_parties)
    payload = call(build_server(service.settings, service), "get_settlement", {"id": FIRST_ID})
    assert error_of(payload)[0] == UNAVAILABLE
    assert (payload["error"]["retry_after"], payload["error"]["retryable"]) == (60, True)
    # Sixty seconds is past the cap: reported, not slept through.
    assert len(transport.requests) == 1


def test_a_refused_credential_crosses_the_server_marked_not_worth_a_retry(
    transport: AppTransport, static_parties: StaticParties
) -> None:
    service = service_over(transport, static_parties, token="wrong-token")
    payload = call(build_server(service.settings, service), "export_settlements", {})
    assert error_of(payload)[0] == UNAVAILABLE
    assert payload["error"]["retryable"] is False
    assert "wrong-token" not in json.dumps(payload)


def test_a_malformed_source_crosses_the_server_naming_the_record(
    static_parties: StaticParties,
) -> None:
    body = edited(FIRST_LINE, surprise=1)
    service = service_over(ScriptedTransport(reply(200, body)), static_parties)
    payload = call(build_server(service.settings, service), "get_settlement", {"id": FIRST_ID})
    assert error_of(payload)[0] == MALFORMED_SOURCE
    assert payload["error"]["record"] == FIRST_ID


def test_an_empty_result_crosses_the_server_as_an_empty_list(offline: ProcessorService) -> None:
    payload = call(
        build_server(offline.settings, offline),
        "list_settlements",
        {"date_from": "2023-01-01", "date_to": "2023-01-02"},
    )
    assert payload == {"records": [], "count": 0, "has_more": False}


def test_a_failed_call_does_not_poison_the_session(static_parties: StaticParties) -> None:
    transport = ScriptedTransport(
        SourceDown("timed out"),
        api_error(401, "invalid_token"),
        reply(200, b"<html>"),
        api_error(429, "rate_limited", retry_after="600"),
        reply(200, FIRST_LINE),
    )
    service = service_over(transport, static_parties)
    server = build_server(service.settings, service)

    async def sequence() -> list[dict[str, Any]]:
        async with Client(server) as client:
            results = [
                await client.call_tool("get_settlement", {"id": FIRST_ID}),
                await client.call_tool("get_settlement", {"id": FIRST_ID}),
                await client.call_tool("get_settlement", {"id": FIRST_ID}),
                await client.call_tool("get_settlement", {"id": FIRST_ID}),
                await client.call_tool("get_settlement", {"id": "nonsense"}),
                await client.call_tool("get_settlement", {"id": FIRST_ID}),
            ]
            assert all(result.is_error is False for result in results)
            return [dict(result.structured_content or {}) for result in results]

    down, unauthorised, garbled, limited, invalid, good = asyncio.run(sequence())
    assert error_of(down)[0] == UNAVAILABLE
    assert error_of(unauthorised)[0] == UNAVAILABLE
    assert error_of(garbled)[0] == MALFORMED_SOURCE
    assert error_of(limited)[0] == UNAVAILABLE
    assert error_of(invalid)[0] == INVALID_ARGUMENT
    assert good["record_id"] == FIRST_ID
    assert TOKEN not in json.dumps([down, unauthorised, garbled, limited, invalid, good])


def test_a_database_dsn_is_never_what_stops_the_settings_from_printing() -> None:
    settings = client_settings(dsn=NO_DATABASE)
    assert "crossfoot_ro" not in repr(settings)
    assert TOKEN not in repr(settings)

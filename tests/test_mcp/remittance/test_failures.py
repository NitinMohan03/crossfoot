"""Bad input and a broken source degrade, they do not crash (plan §4.4 verification).

Both levels are checked, as for the other servers: the tool layer raises a coded `ToolError`,
and the server returns that code in the envelope instead of failing the call. What is specific
to this source is what "broken" means -- a file that is not a PDF, a PDF that is not an advice,
an advice that contradicts itself or is filed under the wrong payment -- and how far the damage
spreads: one unreadable advice fails the calls that ask for it and is *listed*, not hidden, by
the export, which carries on with the rest.
"""

import asyncio
import io
from typing import Any

import pytest
from mcp import Client
from reportlab.pdfgen import canvas

from crossfoot.mcp.common.errors import (
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    MALFORMED_SOURCE,
    NOT_FOUND,
    UNAVAILABLE,
    ToolError,
)
from crossfoot.mcp.remittance import RemittanceService, RemittanceSettings, build_server
from crossfoot.mcp.remittance.parse import NO_HEADER
from crossfoot.mcp.remittance.settings import FX_FILE
from tests.test_mcp.conftest import DEV_SLICE
from tests.test_mcp.remittance.conftest import NO_DATABASE, SMALL, Builder, rendered
from tests.test_mcp.test_failures import call, error_of

FOURTH = "PAY-2024-0004.pdf"
FOURTH_ID = "remit:RA-2024-0004"


def other_document() -> bytes:
    """A real, well-formed PDF that is not a remittance advice."""
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(72, 760, "STATEMENT OF ACCOUNT")
    pdf.drawString(72, 740, "ORMSKIRK NURSERIES PLC")
    pdf.drawString(72, 720, "Balance outstanding: 8717.53")
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


def blank_page() -> bytes:
    """A real PDF with a page and nothing on it: a scan with no text layer looks like this."""
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


class NamesDown:
    """Counterparty names from a database that is not answering."""

    def name_index(self) -> Any:
        raise ToolError(UNAVAILABLE, "the ERP database is not reachable (OperationalError)")

    def registry(self) -> Any:
        raise ToolError(UNAVAILABLE, "the ERP database is not reachable (OperationalError)")

    def close(self) -> None:
        return None


def refused(service: RemittanceService, payment_ref: str = "PAY-2024-0004") -> ToolError:
    with pytest.raises(ToolError) as caught:
        service.get_remittance_for_payment(payment_ref)
    return caught.value


# -- arguments -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payment_ref",
    [
        "INV-2024-0001",
        "remit:RA-2024-0004",
        "PAY-2024-4",
        "PAY-24-0004",
        "pay-2024-0004",
        "PAY-2024-0004.pdf",
        "../erp",
        "PAY-2024-0004/../../erp.sql",
        "PAY-2024-0004\\..\\..\\erp.sql",
        "PAY-2024-0004 PAY-2024-0005",
        "   ",
        "",
    ],
)
def test_what_is_not_a_payment_reference_is_an_invalid_argument(
    offline: RemittanceService, payment_ref: str
) -> None:
    with pytest.raises(ToolError) as caught:
        offline.get_remittance_for_payment(payment_ref)
    assert caught.value.code == INVALID_ARGUMENT


@pytest.mark.parametrize(
    "remittance_id",
    ["PAY-2024-0004", "remit:PAY-2024-0004", "remit:RA-2024-4", "erp:RA-2024-0004", "remit:", ""],
)
def test_what_is_not_a_remittance_id_is_an_invalid_argument(
    offline: RemittanceService, remittance_id: str
) -> None:
    with pytest.raises(ToolError) as caught:
        offline.get_remittance_lines(remittance_id)
    assert caught.value.code == INVALID_ARGUMENT
    with pytest.raises(ToolError) as caught:
        offline.export_remittances(after_id=remittance_id or "x")
    assert caught.value.code == INVALID_ARGUMENT


def test_a_payment_with_no_advice_is_not_found_and_says_that_is_usual(
    offline: RemittanceService,
) -> None:
    with pytest.raises(ToolError) as caught:
        offline.get_remittance_for_payment("PAY-2024-9999")
    assert caught.value.code == NOT_FOUND
    assert "most payments are sent without one" in caught.value.message
    with pytest.raises(ToolError) as caught:
        offline.get_remittance_lines("remit:RA-2024-9999")
    assert caught.value.code == NOT_FOUND
    assert "remit:RA-2024-9999" in caught.value.message


def test_a_limit_below_one_is_an_invalid_argument(offline: RemittanceService) -> None:
    for limit in (0, -5):
        with pytest.raises(ToolError) as caught:
            offline.export_remittances(limit=limit)
        assert caught.value.code == INVALID_ARGUMENT


def test_a_limit_above_the_maximum_says_what_the_maximum_is(offline: RemittanceService) -> None:
    with pytest.raises(ToolError) as caught:
        offline.export_remittances(limit=201)
    assert caught.value.code == LIMIT_EXCEEDED
    assert caught.value.extra == {"max_limit": 200}


# -- a file that is not a readable advice ----------------------------------------------------


@pytest.mark.parametrize(
    ("content", "says"),
    [
        (b"", "the file is empty"),
        (b"Dear Sir, please find our remittance attached.\n", "not a PDF"),
        (b"<html><body>404 Not Found</body></html>", "not a PDF"),
        (b"%PDF-1.4\n" + b"\x00\xff noise " * 50, "could not be read"),
    ],
)
def test_a_file_that_is_not_a_pdf_is_malformed_and_names_the_file(
    broken: Builder, content: bytes, says: str
) -> None:
    error = refused(broken(**{FOURTH: content}))
    assert error.code == MALFORMED_SOURCE
    assert says in error.message
    assert error.extra == {"file": FOURTH}


def test_a_pdf_cut_short_in_transit_is_malformed(broken: Builder) -> None:
    whole = (DEV_SLICE / "remit" / FOURTH).read_bytes()
    error = refused(broken(**{FOURTH: whole[: len(whole) // 2]}))
    assert error.code == MALFORMED_SOURCE
    assert error.extra == {"file": FOURTH}


def test_a_pdf_that_is_not_an_advice_has_no_recognisable_header(broken: Builder) -> None:
    error = refused(broken(**{FOURTH: other_document()}))
    assert error.code == MALFORMED_SOURCE
    assert NO_HEADER in error.message
    assert error.extra == {"file": FOURTH, "page": 1, "row": 1}


def test_a_page_with_no_text_on_it_has_no_recognisable_header(broken: Builder) -> None:
    error = refused(broken(**{FOURTH: blank_page()}))
    assert error.code == MALFORMED_SOURCE
    assert NO_HEADER in error.message
    assert error.extra == {"file": FOURTH, "page": 1, "row": 0}


@pytest.mark.parametrize("layout", ["A", "B", "C"])
def test_an_advice_whose_lines_do_not_add_up_to_its_total_is_malformed(
    broken: Builder, layout: str
) -> None:
    content = rendered(layout, payment_ref="PAY-2024-0004", total_minor=565_051)
    error = refused(broken(**{FOURTH: content}))
    assert error.code == MALFORMED_SOURCE
    assert "the lines add up to 565050" in error.message
    assert "a total of 565051" in error.message
    assert error.extra == {"file": FOURTH, "page": 1, "row": 0}


def test_an_advice_filed_under_another_payment_is_malformed(broken: Builder) -> None:
    # A perfectly good advice for PAY-2024-0099, saved as PAY-2024-0004.pdf.
    error = refused(broken(**{FOURTH: rendered("A", payment_ref="PAY-2024-0099")}))
    assert error.code == MALFORMED_SOURCE
    assert "is an advice for PAY-2024-0099, not for the payment it is filed under" in error.message
    assert error.extra == {"file": FOURTH}


def test_a_file_too_large_to_be_an_advice_is_malformed(broken: Builder) -> None:
    service = broken(**{FOURTH: b"%PDF-1.4\n" + b"0" * 2_100_000})
    error = refused(service)
    assert error.code == MALFORMED_SOURCE
    assert "larger than 2000000 bytes" in error.message


def test_lines_are_refused_from_an_advice_that_does_not_read(broken: Builder) -> None:
    service = broken(**{FOURTH: other_document()})
    with pytest.raises(ToolError) as caught:
        service.get_remittance_lines(FOURTH_ID)
    assert caught.value.code == MALFORMED_SOURCE


def test_a_refusal_is_remembered_and_says_the_same_thing_each_time(broken: Builder) -> None:
    service = broken(**{FOURTH: other_document()})
    first, second = refused(service), refused(service)
    assert first is not second
    assert (first.code, first.message, first.extra) == (second.code, second.message, second.extra)
    # Raised afresh each time, not one exception gathering tracebacks.
    assert second.__traceback__ is not None and second.__traceback__ is not first.__traceback__


# -- how far the damage spreads --------------------------------------------------------------


def test_one_unreadable_advice_does_not_take_the_others_with_it(broken: Builder) -> None:
    service = broken(**{FOURTH: other_document()})
    assert refused(service).code == MALFORMED_SOURCE
    for payment_ref in SMALL:
        if payment_ref != "PAY-2024-0004":
            assert service.get_remittance_for_payment(payment_ref).reference == payment_ref


def test_the_export_lists_what_it_could_not_read_and_carries_on(broken: Builder) -> None:
    service = broken(
        **{
            FOURTH: other_document(),
            "PAY-2024-0002.pdf": b"not a pdf",
            "PAY-2024-0500.pdf": rendered("B", payment_ref="PAY-2024-0500", total_minor=1),
        }
    )
    page = service.export_remittances()
    assert [record.record_id for record in page.records] == [
        "remit:RA-2024-0001",
        "remit:RA-2024-0003",
        "remit:RA-2024-0027",
    ]
    assert [(item.remittance_id, item.payment_ref) for item in page.unparsed] == [
        ("remit:RA-2024-0002", "PAY-2024-0002"),
        (FOURTH_ID, "PAY-2024-0004"),
        ("remit:RA-2024-0500", "PAY-2024-0500"),
    ]
    assert {item.error.code for item in page.unparsed} == {MALFORMED_SOURCE}
    assert [item.error.extra["file"] for item in page.unparsed] == [
        "PAY-2024-0002.pdf",
        FOURTH,
        "PAY-2024-0500.pdf",
    ]
    assert (page.examined, page.has_more, page.next_after_id) == (6, False, "remit:RA-2024-0500")


def test_paging_moves_past_an_advice_it_could_not_read(broken: Builder) -> None:
    service = broken(**{"PAY-2024-0002.pdf": b"not a pdf", "PAY-2024-0003.pdf": b"nor this"})
    seen: list[str] = []
    unread: list[str] = []
    after: str | None = None
    sizes: list[tuple[int, int]] = []
    while True:
        page = service.export_remittances(after_id=after, limit=2)
        sizes.append((len(page.records), len(page.unparsed)))
        seen += [record.record_id for record in page.records]
        unread += [item.remittance_id for item in page.unparsed]
        if not page.has_more:
            break
        after = page.next_after_id
    # The second page holds no record at all and is still not the end.
    assert sizes == [(1, 1), (1, 1), (1, 0)]
    assert seen == ["remit:RA-2024-0001", "remit:RA-2024-0004", "remit:RA-2024-0027"]
    assert unread == ["remit:RA-2024-0002", "remit:RA-2024-0003"]


def test_a_dataset_with_no_advices_is_an_empty_export_not_an_error(broken: Builder) -> None:
    service = broken(**{f"{payment_ref}.pdf": None for payment_ref in SMALL})
    page = service.export_remittances()
    assert (page.records, page.unparsed, page.next_after_id, page.has_more) == ((), (), None, False)
    with pytest.raises(ToolError) as caught:
        service.get_remittance_for_payment("PAY-2024-0001")
    assert caught.value.code == NOT_FOUND


def test_a_missing_dataset_is_unavailable_from_every_tool(static_parties: Any) -> None:
    service = RemittanceService(
        RemittanceSettings(data_dir=DEV_SLICE / "no-such-dataset", dsn=NO_DATABASE),
        parties=static_parties,
    )
    for attempt in (
        lambda: service.get_remittance_for_payment("PAY-2024-0001"),
        lambda: service.get_remittance_lines("remit:RA-2024-0001"),
        lambda: service.export_remittances(),
    ):
        with pytest.raises(ToolError) as caught:
            attempt()
        assert caught.value.code == UNAVAILABLE
        assert "just seed" in caught.value.message


# -- the rate table --------------------------------------------------------------------------


def test_without_a_rate_table_a_foreign_advice_is_unavailable_and_sterling_ones_are_served(
    broken: Builder,
) -> None:
    service = broken(**{FX_FILE: None})
    error = refused(service, "PAY-2024-0027")
    assert error.code == UNAVAILABLE
    assert error.extra == {"file": FX_FILE}
    assert "EUR" in error.message
    assert service.get_remittance_for_payment("PAY-2024-0004").amount.currency == "GBP"
    # Not a fact about that one advice, so the export does not file it under `unparsed`.
    with pytest.raises(ToolError) as caught:
        service.export_remittances()
    assert caught.value.code == UNAVAILABLE
    assert len(service.export_remittances(limit=4).records) == 4


def test_a_rate_table_that_does_not_parse_is_malformed_and_names_itself(broken: Builder) -> None:
    service = broken(**{FX_FILE: b"date,USD,EUR\n2024-02-16,1.27,n/a\n"})
    error = refused(service, "PAY-2024-0027")
    assert error.code == MALFORMED_SOURCE
    assert error.extra == {"file": FX_FILE, "row": 1}
    assert service.get_remittance_for_payment("PAY-2024-0004").amount.base_minor == 871_753


def test_a_rate_table_that_stops_before_the_payment_date_is_malformed(broken: Builder) -> None:
    service = broken(**{FX_FILE: b"date,USD,EUR\n2024-02-15,1.27,1.16\n"})
    error = refused(service, "PAY-2024-0027")
    assert error.code == MALFORMED_SOURCE
    assert "no EUR rate for 2024-02-16" in error.message
    assert error.extra == {"file": FX_FILE}
    # The export says which advice could not be valued, and still serves the other four.
    page = service.export_remittances()
    assert len(page.records) == 4
    assert [item.remittance_id for item in page.unparsed] == ["remit:RA-2024-0027"]


# -- the other source: names from Postgres ---------------------------------------------------


def test_names_being_down_is_unavailable_not_an_unresolved_party(broken: Builder) -> None:
    # The advice is on disk and readable. It is still not served: an advice that reads as
    # coming from nobody, because the name list was down, would be stored as exactly that.
    service = broken(parties=NamesDown())
    assert refused(service).code == UNAVAILABLE
    # And it fails the export outright rather than being one advice's problem.
    with pytest.raises(ToolError) as caught:
        service.export_remittances()
    assert caught.value.code == UNAVAILABLE
    # What the advice says needs no name list.
    assert service.get_remittance_lines(FOURTH_ID).total_minor == 871_753
    # Nothing was remembered as broken: when the names come back, so does the advice.
    assert service._parsed["PAY-2024-0004"].payment_ref == "PAY-2024-0004"  # type: ignore[union-attr]


def test_an_unreachable_database_is_unavailable_and_does_not_leak_the_dsn(
    developed_dir: Any,
) -> None:
    service = RemittanceService(
        RemittanceSettings(data_dir=developed_dir, dsn=NO_DATABASE, connect_timeout=2)
    )
    try:
        error = refused(service)
        assert error.code == UNAVAILABLE
        # The DSN carries a password and must not appear in what the agent is told.
        assert "crossfoot_ro" not in error.message
    finally:
        service.close()


def test_a_schema_with_no_counterparties_is_unavailable_and_says_what_to_run(
    remittance_settings: RemittanceSettings,
) -> None:
    absent = RemittanceSettings(
        data_dir=remittance_settings.data_dir,
        dsn=remittance_settings.dsn,
        erp_schema="erp_does_not_exist",
    )
    service = RemittanceService(absent)
    try:
        error = refused(service)
        assert error.code == UNAVAILABLE
        assert "db-load" in error.message
    finally:
        service.close()


def test_settings_that_cannot_work_are_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="not a usable schema name"):
        RemittanceSettings(dsn="postgresql://x/y", erp_schema="erp; DROP SCHEMA public")
    with pytest.raises(ValueError, match="limits must satisfy"):
        RemittanceSettings(dsn="postgresql://x/y", default_limit=50, max_limit=10)
    with pytest.raises(ValueError, match="max_bytes and max_pages"):
        RemittanceSettings(dsn="postgresql://x/y", max_pages=0)


# -- the server returns that code, and keeps working -----------------------------------------


def test_the_server_returns_the_envelope_instead_of_failing_the_call(
    offline: RemittanceService,
) -> None:
    server = build_server(offline.settings, offline)
    missing = call(server, "get_remittance_for_payment", {"payment_ref": "PAY-2024-9999"})
    assert error_of(missing)[0] == NOT_FOUND
    invalid = call(server, "get_remittance_for_payment", {"payment_ref": "../erp"})
    assert error_of(invalid)[0] == INVALID_ARGUMENT
    not_an_id = call(server, "get_remittance_lines", {"remittance_id": "PAY-2024-0004"})
    assert error_of(not_an_id)[0] == INVALID_ARGUMENT
    too_many = call(server, "export_remittances", {"limit": 10_000})
    assert error_of(too_many)[0] == LIMIT_EXCEEDED
    assert too_many["error"]["max_limit"] == 200


def test_a_malformed_advice_crosses_the_server_as_a_code_with_its_file_page_and_row(
    broken: Builder,
) -> None:
    service = broken(**{FOURTH: other_document()})
    server = build_server(service.settings, service)
    for name, arguments in (
        ("get_remittance_for_payment", {"payment_ref": "PAY-2024-0004"}),
        ("get_remittance_lines", {"remittance_id": FOURTH_ID}),
    ):
        payload = call(server, name, arguments)
        assert error_of(payload)[0] == MALFORMED_SOURCE
        assert (payload["error"]["file"], payload["error"]["page"], payload["error"]["row"]) == (
            FOURTH,
            1,
            1,
        )


def test_the_export_crosses_the_server_with_its_unparsed_list(broken: Builder) -> None:
    service = broken(**{FOURTH: other_document()})
    payload = call(build_server(service.settings, service), "export_remittances", {})
    assert "error" not in payload
    assert (payload["count"], payload["examined"], payload["has_more"]) == (4, 5, False)
    assert payload["next_after_id"] == "remit:RA-2024-0027"
    (unparsed,) = payload["unparsed"]
    assert (unparsed["remittance_id"], unparsed["payment_ref"]) == (FOURTH_ID, "PAY-2024-0004")
    assert unparsed["error"]["code"] == MALFORMED_SOURCE
    assert unparsed["error"]["file"] == FOURTH
    assert NO_HEADER in unparsed["error"]["message"]


def test_an_unavailable_source_crosses_the_server_as_a_code(static_parties: Any) -> None:
    settings = RemittanceSettings(data_dir=DEV_SLICE / "no-such-dataset", dsn=NO_DATABASE)
    server = build_server(settings, RemittanceService(settings, parties=static_parties))
    assert error_of(call(server, "export_remittances", {}))[0] == UNAVAILABLE


def test_an_empty_page_crosses_the_server_as_an_empty_list(offline: RemittanceService) -> None:
    payload = call(
        build_server(offline.settings, offline),
        "export_remittances",
        {"after_id": "remit:RA-2024-0525"},
    )
    assert payload == {
        "records": [],
        "count": 0,
        "unparsed": [],
        "examined": 0,
        "next_after_id": None,
        "has_more": False,
    }


def test_a_failed_call_does_not_poison_the_session(broken: Builder) -> None:
    service = broken(**{FOURTH: other_document(), "PAY-2024-0002.pdf": b"not a pdf"})
    server = build_server(service.settings, service)

    async def sequence() -> list[dict[str, Any]]:
        async with Client(server) as client:
            results = [
                await client.call_tool(
                    "get_remittance_for_payment", {"payment_ref": "PAY-2024-0004"}
                ),
                await client.call_tool(
                    "get_remittance_for_payment", {"payment_ref": "PAY-2024-0002"}
                ),
                await client.call_tool("get_remittance_for_payment", {"payment_ref": "nonsense"}),
                await client.call_tool(
                    "get_remittance_lines", {"remittance_id": "remit:RA-2024-9999"}
                ),
                await client.call_tool("export_remittances", {"limit": 500}),
                await client.call_tool(
                    "get_remittance_for_payment", {"payment_ref": "PAY-2024-0001"}
                ),
                await client.call_tool(
                    "get_remittance_lines", {"remittance_id": "remit:RA-2024-0003"}
                ),
                await client.call_tool("export_remittances", {}),
            ]
            assert all(result.is_error is False for result in results)
            return [dict(result.structured_content or {}) for result in results]

    no_header, not_pdf, invalid, missing, too_many, good, lines, export = asyncio.run(sequence())
    assert error_of(no_header)[0] == MALFORMED_SOURCE
    assert error_of(not_pdf)[0] == MALFORMED_SOURCE
    assert error_of(invalid)[0] == INVALID_ARGUMENT
    assert error_of(missing)[0] == NOT_FOUND
    assert error_of(too_many)[0] == LIMIT_EXCEEDED
    # Everything after five refusals, on the same session, still works.
    assert good["record_id"] == "remit:RA-2024-0001"
    assert lines["count"] == 2
    assert (export["count"], len(export["unparsed"])) == (3, 2)

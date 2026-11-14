"""The parse rate on the held-out layout (spec 01 §6; plan §4.4 verification).

One remittance layout in four is reserved. The parser was developed against the other three --
`conftest.py` keeps the held-out PDFs out of every other test's reach -- and this file is the
only place they are opened. It reports what the parser makes of a page design it was not
written for, and that number goes in the README whatever it is.

**The number: 0 of 81 parsed. 0 misread. 81 refused.**

Three outcomes are counted, because the two ways of not being right are not the same:

* **exact** -- the advice parsed and every field equals `remit.jsonl`;
* **wrong** -- it parsed, and something differs: a misread that would reach the ledger;
* **refused** -- `malformed_source`: the server said it could not read the page.

All 81 are refused, at the same place and for the same stated reason: the page has no row of
column headings the reader knows. The stage below the form reader carried over untouched --
every held-out PDF becomes text runs, one page each, opening with the title of a remittance
advice -- so what did not generalise is the vocabulary of the form, not the reading of the PDF.

On the 444 advices in the developed layouts the same parser is exact on 444. So across the
slice: 444 of 525 advices read (84.6%), none read wrongly, and 81 handed to a person by name.

**How this number was produced, so it means what it says.** The parser and the server were
committed first; the measurement was then run once and recorded here without the parser being
touched. `MEASURED_AGAINST` is a fingerprint of the two modules that do the reading. If either
changes, the test below fails -- not because changing them is wrong, but because the number
above is then a statement about code that no longer exists and has to be measured again, and
the README has to say the parser changed after its author had seen this result.

One thing the protocol could not undo: the session that wrote the parser had been told to read
the generator's `remit_pdf.py`, which draws all four layouts, so its author knew how the
held-out layout differs before measuring. No held-out PDF was opened, and no rule was added
that the developed layouts do not exercise; the result -- nothing parsed -- is at least
consistent with that. It is stated here because a held-out number is only as good as the
holding out.
"""

import asyncio
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

import crossfoot.mcp.remittance
from crossfoot.config import HELD_OUT_TEMPLATE
from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError
from crossfoot.mcp.common.parties import StaticParties
from crossfoot.mcp.remittance import RemittanceService, RemittanceSettings, build_server
from crossfoot.mcp.remittance.layout import read_pages
from crossfoot.mcp.remittance.parse import TITLE, parse_advice
from crossfoot.mcp.remittance.settings import MAX_PAGES
from tests.test_mcp.conftest import DEV_SLICE
from tests.test_mcp.remittance.conftest import (
    ADVICES,
    DEVELOPED,
    HELD_OUT,
    NO_DATABASE,
    REMIT_DIR,
    is_developed,
)
from tests.test_mcp.remittance.test_parse import FIELDS, as_truth

#: The result. These three numbers are the ones the README reports.
EXACT, WRONG, REFUSED = 0, 0, 81

#: sha256 of `layout.py` + `parse.py` (line endings normalised) as they stood when measured.
MEASURED_AGAINST = "29c1ecebf73125fc9543e709092d149e31e0e7ae934ad4ad6dfbf9d1f891c14d"

#: What every held-out advice is refused for.
REASON = "no line-item table"


@pytest.fixture(scope="module")
def held_out(truth: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in truth if not is_developed(row)]


@pytest.fixture(scope="module")
def outcomes(held_out: list[dict[str, Any]]) -> dict[str, tuple[str, ToolError | None]]:
    """Payment reference -> (exact | wrong | refused, the refusal if there was one)."""
    out: dict[str, tuple[str, ToolError | None]] = {}
    for row in held_out:
        name = f"{row['payment_ref']}.pdf"
        raw = (REMIT_DIR / name).read_bytes()
        try:
            advice = parse_advice(name, read_pages(name, raw, max_pages=MAX_PAGES))
        except ToolError as exc:
            out[row["payment_ref"]] = ("refused", exc)
            continue
        got = as_truth(advice)
        same = all(got[field] == row[field] for field in FIELDS)
        out[row["payment_ref"]] = ("exact" if same else "wrong", None)
    return out


@pytest.fixture(scope="module")
def whole_slice(static_parties: StaticParties) -> RemittanceService:
    """The server over the committed directory as it is: all four layouts."""
    return RemittanceService(
        RemittanceSettings(data_dir=DEV_SLICE, dsn=NO_DATABASE), parties=static_parties
    )


def test_the_held_out_layout_is_the_one_the_generator_reserved(
    held_out: list[dict[str, Any]],
) -> None:
    assert HELD_OUT_TEMPLATE == "D"
    assert len(held_out) == HELD_OUT == 81
    assert {row["template"] for row in held_out} == {HELD_OUT_TEMPLATE}


def test_the_parse_rate_on_the_held_out_layout(
    outcomes: dict[str, tuple[str, ToolError | None]],
) -> None:
    tally = Counter(outcome for outcome, _ in outcomes.values())
    assert (tally["exact"], tally["wrong"], tally["refused"]) == (EXACT, WRONG, REFUSED)
    assert sum(tally.values()) == HELD_OUT


def test_nothing_on_the_held_out_layout_is_misread(
    outcomes: dict[str, tuple[str, ToolError | None]],
) -> None:
    # The property that matters more than the rate: an unseen layout is not guessed at. A
    # refusal goes to a person; a misread would go into the ledger looking like an answer.
    assert [ref for ref, (outcome, _) in outcomes.items() if outcome == "wrong"] == []


def test_every_refusal_is_coded_and_says_the_same_true_thing(
    outcomes: dict[str, tuple[str, ToolError | None]],
) -> None:
    refusals = {ref: error for ref, (_, error) in outcomes.items() if error is not None}
    assert len(refusals) == REFUSED
    for ref, error in refusals.items():
        assert error.code == MALFORMED_SOURCE
        assert error.extra == {"file": f"{ref}.pdf", "page": 1, "row": 0}
        assert REASON in error.message


def test_the_layer_under_the_form_reader_carried_over(held_out: list[dict[str, Any]]) -> None:
    # Every held-out PDF is a readable PDF: one page of text runs, opening with the title.
    for row in held_out:
        name = f"{row['payment_ref']}.pdf"
        pages = read_pages(name, (REMIT_DIR / name).read_bytes(), max_pages=MAX_PAGES)
        assert len(pages) == 1
        assert pages[0].rows[0].texts == (TITLE,)


def test_the_parser_is_the_one_the_number_was_measured_on() -> None:
    package = Path(crossfoot.mcp.remittance.__file__).parent
    digest = hashlib.sha256()
    for name in ("layout.py", "parse.py"):
        digest.update((package / name).read_bytes().replace(b"\r\n", b"\n"))
    assert digest.hexdigest() == MEASURED_AGAINST, (
        "layout.py or parse.py changed after the held-out measurement. Run this file, update "
        "EXACT / WRONG / REFUSED and the README from what it now reports, keep the original "
        "0 / 0 / 81 beside the new number there, and say that the parser changed after its "
        "author had seen the held-out result. Then update MEASURED_AGAINST."
    )


# -- what the server does with a slice that holds all four layouts ---------------------------


def test_the_export_serves_what_it_can_read_and_names_what_it_cannot(
    whole_slice: RemittanceService, truth: list[dict[str, Any]]
) -> None:
    records: list[str] = []
    unparsed: list[str] = []
    examined: list[int] = []
    after: str | None = None
    while True:
        page = whole_slice.export_remittances(after_id=after, limit=200)
        records += [record.record_id for record in page.records]
        unparsed += [item.remittance_id for item in page.unparsed]
        assert {item.error.code for item in page.unparsed} <= {MALFORMED_SOURCE}
        examined.append(page.examined)
        if not page.has_more:
            break
        after = page.next_after_id
    assert examined == [200, 200, 125]
    assert (len(records), len(unparsed)) == (DEVELOPED, HELD_OUT)
    assert len(records) + len(unparsed) == ADVICES
    # Exactly the developed advices are served and exactly the held-out ones are listed.
    assert records == [row["id"] for row in truth if is_developed(row)]
    assert unparsed == [row["id"] for row in truth if not is_developed(row)]
    # 84.6% of the slice's advices read; the rest are a list of 81 names, not a silence.
    assert round(100 * len(records) / ADVICES, 1) == 84.6


def test_asking_for_a_held_out_advice_answers_with_a_code_and_the_session_survives(
    whole_slice: RemittanceService, held_out: list[dict[str, Any]]
) -> None:
    server = build_server(whole_slice.settings, whole_slice)
    first = held_out[0]

    async def sequence() -> list[dict[str, Any]]:
        async with Client(server) as client:
            results = [
                await client.call_tool(
                    "get_remittance_for_payment", {"payment_ref": first["payment_ref"]}
                ),
                await client.call_tool("get_remittance_lines", {"remittance_id": first["id"]}),
                await client.call_tool(
                    "get_remittance_for_payment", {"payment_ref": "PAY-2024-0004"}
                ),
            ]
            assert all(result.is_error is False for result in results)
            return [dict(result.structured_content or {}) for result in results]

    record, lines, good = asyncio.run(sequence())
    for payload in (record, lines):
        assert payload["error"]["code"] == MALFORMED_SOURCE
        assert payload["error"]["file"] == f"{first['payment_ref']}.pdf"
        assert REASON in payload["error"]["message"]
    assert good["record_id"] == "remit:RA-2024-0004"

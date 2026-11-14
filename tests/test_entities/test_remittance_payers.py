"""Which resolver reads an advice's payer name, decided by measurement.

The header of a remittance advice names the payer and nothing else -- no reference rides on the
end of it -- so `resolve`, the ladder for "a name that is only a name", is the obvious choice.
It was the obvious choice for the processor's `payer_name` too, and there it was measured wrong
on payers who shorten their name to its first word. Session 2.4 measured both again before
picking one, on the third source.

The names graded are the generator's own (`remit.jsonl`), all 525 of them, so the measurement
does not depend on the PDF parser; `tests/test_mcp/remittance/test_parse.py` separately shows
the parser reads those names character for character. The truth is the manifest's: an advice is
evidence for a group, and its payer is the counterparty of that group's invoices. The alias
registry is empty throughout, so this measures the string layers alone.

| resolver                | correct | wrong | unresolved |
| ----------------------- | ------- | ----- | ---------- |
| `resolve`               | 508     | 4     | 13         |
| `resolve_description`   | 499     | 2     | 24         |

`mcp-remittance` therefore uses `resolve_description`, as the other two payment-side servers do.
The two it puts right are `JARVIS` -- a lone first word handed to another counterparty at a
fuzzy score of 100, the same failure as on the bank and the processor -- and a typo'd name
matched at 89 to a company with a similar one. The price is nine lone first words (`CALDER`,
`AVONDALE`) that `resolve` happened to get right and the stricter score leaves for review.
Unresolved goes to the registry or to a person; wrong goes into the ledger. The two answers
still wrong are not fuzzy matches at all -- see the last test.

Every answer that is not simply correct is in the `name` and `dba` classes, and none of those
advices happens to use the held-out layout, so the numbers the server actually produces differ
from these only by 79 fewer correct answers and two fewer parent-company names.
"""

from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from crossfoot.config import HELD_OUT_TEMPLATE
from crossfoot.entities import (
    AliasRegistry,
    Candidate,
    NameIndex,
    resolve,
    resolve_description,
)
from crossfoot.generate.read import read_erp, read_manifest, read_remit
from crossfoot.models.canonical import PartyRef

DEV_SLICE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "dev"

GRADED = 525
DEVELOPED = 444

#: class, verdict, the party the payer name resolved to, and whether the layout is held out.
Grade = tuple[str, str, PartyRef, bool]
Resolver = Callable[[str, NameIndex, AliasRegistry], PartyRef]


def grade(resolver: Resolver) -> dict[str, Grade]:
    """Every advice -> how its payer name fared."""
    erp = read_erp(DEV_SLICE / "erp.sql")
    index = NameIndex(
        Candidate(counterparty_id=cp.id, legal_name=cp.legal_name, trading_name=cp.trading_name)
        for cp in erp.counterparties
    )
    invoices = erp.invoice_by_ref()
    _, groups = read_manifest(DEV_SLICE / "manifest.jsonl")
    group_of = {
        evidence: group
        for group in groups
        for evidence in group.get("evidence_ids", [])
        if evidence.startswith("remit:")
    }

    registry = AliasRegistry.empty()
    out: dict[str, Grade] = {}
    for advice in read_remit(DEV_SLICE / "remit"):
        group = group_of[advice["id"]]
        payers = {
            invoices[source.removeprefix("erp:")].counterparty_id
            for source in group["source_ids"]
            if source.startswith("erp:INV")
        }
        party = resolver(advice["payer"], index, registry)
        if party.counterparty_id is None:
            verdict = "unresolved"
        else:
            verdict = "correct" if party.counterparty_id in payers else "wrong"
        out[advice["id"]] = (
            group.get("reasoning_subtype") or group["match_type"],
            verdict,
            party,
            advice["template"] == HELD_OUT_TEMPLATE,
        )
    return out


@pytest.fixture(scope="module")
def by_name() -> dict[str, Grade]:
    return grade(resolve)


@pytest.fixture(scope="module")
def by_description() -> dict[str, Grade]:
    return grade(resolve_description)


def tally(graded: dict[str, Grade], *classes: str, developed_only: bool = False) -> Counter[str]:
    return Counter(
        verdict
        for kind, verdict, _, held_out in graded.values()
        if (not classes or kind in classes) and not (developed_only and held_out)
    )


def test_every_advice_can_be_graded(
    by_name: dict[str, Grade], by_description: dict[str, Grade]
) -> None:
    assert len(by_name) == len(by_description) == GRADED
    assert sum(1 for grade in by_name.values() if not grade[3]) == DEVELOPED


def test_the_headline_numbers(by_name: dict[str, Grade], by_description: dict[str, Grade]) -> None:
    assert tally(by_name) == {"correct": 508, "wrong": 4, "unresolved": 13}
    assert tally(by_description) == {"correct": 499, "wrong": 2, "unresolved": 24}


def test_the_numbers_on_the_advices_the_server_can_read(
    by_name: dict[str, Grade], by_description: dict[str, Grade]
) -> None:
    # The held-out layout's 81 advices are 79 correct answers and two parent-company names.
    assert tally(by_name, developed_only=True) == {"correct": 429, "wrong": 4, "unresolved": 11}
    assert tally(by_description, developed_only=True) == {
        "correct": 420,
        "wrong": 2,
        "unresolved": 22,
    }


def test_the_two_differ_only_on_drifted_names(
    by_name: dict[str, Grade], by_description: dict[str, Grade]
) -> None:
    assert tally(by_name, "name") == {"correct": 33, "wrong": 4, "unresolved": 1}
    assert tally(by_description, "name") == {"correct": 24, "wrong": 2, "unresolved": 12}
    # A parent-company name is out of any string metric's reach either way (spec 01 §4.8a).
    assert tally(by_name, "dba") == tally(by_description, "dba") == {"unresolved": 12}
    # Every other class is an untouched or merely shortened name, and both get all of them.
    for graded in (by_name, by_description):
        others = [item for item in graded.values() if item[0] not in ("name", "dba")]
        assert len(others) == 475
        assert {verdict for _, verdict, _, _ in others} == {"correct"}
        assert {party.method for _, _, party, _ in others} == {"exact", "normalised"}


def test_what_resolve_gets_wrong(by_name: dict[str, Grade]) -> None:
    wrong = [party for _, verdict, party, _ in by_name.values() if verdict == "wrong"]
    assert Counter((party.surface, party.method, party.similarity) for party in wrong) == {
        ("JARVIS", "fuzzy", 100): 1,
        ("PAZRICK FABRICATION LIMITED", "fuzzy", 89): 1,
        ("EASTGATE", "exact", 100): 2,
    }


def test_the_stricter_score_abstains_on_both_fuzzy_mistakes_and_on_nine_lone_words(
    by_description: dict[str, Grade],
) -> None:
    abstained = Counter(
        party.surface
        for kind, verdict, party, _ in by_description.values()
        if kind == "name" and verdict == "unresolved"
    )
    assert abstained == {
        "JARVIS": 1,
        "PAZRICK FABRICATION LIMITED": 1,
        "MERIDIAN": 1,
        "CALDER": 1,
        "WHITFIELD": 1,
        "AVONDALE": 2,
        "GLENDOWER": 2,
        "TALBOT": 1,
        "LINDSEY": 1,
        "NETHERBY": 1,
    }
    # No fuzzy match that is left rests on an inflated score, and every one is right.
    fuzzy = [item for item in by_description.values() if item[2].method == "fuzzy"]
    assert len(fuzzy) == 11
    assert {verdict for _, verdict, _, _ in fuzzy} == {"correct"}
    assert max(party.similarity for _, _, party, _ in fuzzy) < 100


def test_the_answers_still_wrong_are_ones_no_string_rule_can_fix(
    by_description: dict[str, Grade],
) -> None:
    wrong = {
        advice_id: party
        for advice_id, (_, verdict, party, _) in by_description.items()
        if verdict == "wrong"
    }
    # `EASTGATE` is, character for character, another counterparty's trading name. The lines
    # the advice lists are what tell the two apart, and that is the matcher's work.
    assert {advice_id: party.surface for advice_id, party in wrong.items()} == {
        "remit:RA-2024-0093": "EASTGATE",
        "remit:RA-2024-0260": "EASTGATE",
    }
    assert {party.method for party in wrong.values()} == {"exact"}


def test_no_drifted_or_parent_name_sits_on_a_held_out_advice_except_two_parents(
    by_description: dict[str, Grade],
) -> None:
    held_out = Counter(
        (kind, verdict) for kind, verdict, _, is_held_out in by_description.values() if is_held_out
    )
    assert sum(held_out.values()) == GRADED - DEVELOPED
    assert held_out[("dba", "unresolved")] == 2
    assert {verdict for (_, verdict), _ in held_out.items()} == {"correct", "unresolved"}
    assert sum(count for (_, verdict), count in held_out.items() if verdict == "correct") == 79

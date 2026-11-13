"""Which resolver reads a settlement line's `payer_name`, decided by measurement.

`payer_name` is a field of its own -- no reference rides on the end of it, as one does on a bank
description -- so `resolve`, the ladder for "a name that is only a name", is the obvious choice.
Session 2.2 found that `resolve` has an exposure `resolve_description` does not: its fuzzy layer
gates on `token_set_ratio`, which scores a subset 100, so a payer who shortens their name to its
first word matches every counterparty that starts with that word. Session 2.3 measured both on
the dev slice before picking one.

The truth is the manifest's: a settlement line that settles an invoice belongs to that invoice's
counterparty, and a duplicate belongs to whoever its original does. All 552 lines can be graded.
The alias registry is empty throughout, so this measures the string layers alone.

| resolver                | correct | wrong | unresolved |
| ----------------------- | ------- | ----- | ---------- |
| `resolve`               | 535     | 7     | 10         |
| `resolve_description`   | 529     | 1     | 22         |

`mcp-processor` therefore uses `resolve_description`. Six of `resolve`'s seven wrong answers are
a lone first word (`NEWCOMEN`, `JARVIS`) handed to a counterparty at a fuzzy score of 100; the
stricter per-prefix score leaves those six unresolved, and six that `resolve` happened to get
right go unresolved with them. Unresolved goes to the registry or to review; wrong goes into the
ledger. The one answer still wrong is not a fuzzy match at all -- see the last test.
"""

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from crossfoot.entities import (
    AliasRegistry,
    Candidate,
    NameIndex,
    resolve,
    resolve_description,
)
from crossfoot.generate.read import read_erp, read_manifest
from crossfoot.models.canonical import PartyRef

DEV_SLICE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "dev"

GRADED = 552

Grade = tuple[str, str, PartyRef]
Resolver = Callable[[str, NameIndex, AliasRegistry], PartyRef]


def grade(resolver: Resolver) -> dict[str, Grade]:
    """Every settlement line -> (class, verdict, the party its payer name resolved to)."""
    erp = read_erp(DEV_SLICE / "erp.sql")
    index = NameIndex(
        Candidate(counterparty_id=cp.id, legal_name=cp.legal_name, trading_name=cp.trading_name)
        for cp in erp.counterparties
    )
    invoices = erp.invoice_by_ref()
    text = (DEV_SLICE / "processor" / "settlements.jsonl").read_text(encoding="utf-8")
    names = {
        line["id"]: line["payer_name"]
        for line in (json.loads(raw) for raw in text.splitlines() if raw.strip())
    }
    _, groups = read_manifest(DEV_SLICE / "manifest.jsonl")

    truth: dict[str, set[int]] = {}
    label: dict[str, str] = {}
    for group in groups:
        payers = {
            invoices[source.removeprefix("erp:")].counterparty_id
            for source in group["source_ids"]
            if source.startswith("erp:INV")
        }
        for target in group["target_ids"]:
            if not target.startswith("proc:"):
                continue
            label[target] = group.get("reasoning_subtype") or group["match_type"]
            if payers:
                truth[target] = payers
    for group in groups:
        original = group.get("duplicate_of")
        if group["match_type"] == "duplicate" and original in truth:
            for target in group["target_ids"]:
                truth[target] = truth[original]

    registry = AliasRegistry.empty()
    out: dict[str, Grade] = {}
    for line_id, payers in truth.items():
        party = resolver(names[line_id], index, registry)
        if party.counterparty_id is None:
            verdict = "unresolved"
        else:
            verdict = "correct" if party.counterparty_id in payers else "wrong"
        out[line_id] = (label[line_id], verdict, party)
    return out


@pytest.fixture(scope="module")
def by_name() -> dict[str, Grade]:
    return grade(resolve)


@pytest.fixture(scope="module")
def by_description() -> dict[str, Grade]:
    return grade(resolve_description)


def tally(graded: dict[str, Grade], *classes: str) -> Counter[str]:
    return Counter(
        verdict for kind, verdict, _ in graded.values() if not classes or kind in classes
    )


def test_every_settlement_line_can_be_graded(
    by_name: dict[str, Grade], by_description: dict[str, Grade]
) -> None:
    assert len(by_name) == len(by_description) == GRADED


def test_the_headline_numbers(by_name: dict[str, Grade], by_description: dict[str, Grade]) -> None:
    assert tally(by_name) == {"correct": 535, "wrong": 7, "unresolved": 10}
    assert tally(by_description) == {"correct": 529, "wrong": 1, "unresolved": 22}


def test_the_two_differ_only_on_drifted_names(
    by_name: dict[str, Grade], by_description: dict[str, Grade]
) -> None:
    assert tally(by_name, "name") == {"correct": 25, "wrong": 7}
    assert tally(by_description, "name") == {"correct": 19, "wrong": 1, "unresolved": 12}
    # A parent-company name is out of any string metric's reach either way (spec 01 §4.8a).
    assert tally(by_name, "dba") == tally(by_description, "dba") == {"unresolved": 10}
    # Every other class is an untouched or merely shortened name, and both get all of them.
    for graded in (by_name, by_description):
        others = [item for item in graded.values() if item[0] not in ("name", "dba")]
        assert len(others) == 510
        assert {verdict for _, verdict, _ in others} == {"correct"}
        assert {party.method for _, _, party in others} == {"exact", "normalised"}


def test_what_resolve_gets_wrong_is_a_lone_word_scored_as_a_perfect_match(
    by_name: dict[str, Grade],
) -> None:
    wrong = [party for _, verdict, party in by_name.values() if verdict == "wrong"]
    assert Counter(party.surface for party in wrong) == {"NEWCOMEN": 4, "JARVIS": 2, "PENNINE": 1}
    fuzzy = [party for party in wrong if party.method == "fuzzy"]
    assert len(fuzzy) == 6
    assert {party.similarity for party in fuzzy} == {100}


def test_the_stricter_score_abstains_on_exactly_those(by_description: dict[str, Grade]) -> None:
    abstained = Counter(
        party.surface
        for kind, verdict, party in by_description.values()
        if kind == "name" and verdict == "unresolved"
    )
    assert abstained["NEWCOMEN"] == 4
    assert abstained["JARVIS"] == 2
    # No fuzzy match that is left rests on an inflated score, and every one is right.
    fuzzy = [item for item in by_description.values() if item[2].method == "fuzzy"]
    assert len(fuzzy) == 11
    assert {verdict for _, verdict, _ in fuzzy} == {"correct"}
    assert max(party.similarity for _, _, party in fuzzy) < 100


def test_the_one_answer_still_wrong_is_one_no_string_rule_can_fix(
    by_description: dict[str, Grade],
) -> None:
    wrong = {
        line_id: party
        for line_id, (_, verdict, party) in by_description.items()
        if verdict == "wrong"
    }
    # `PENNINE` is, character for character, another counterparty's trading name. The amount
    # and the reference are what tell the two apart, and that is the matcher's work.
    assert {line_id: party.surface for line_id, party in wrong.items()} == {
        "proc:stl_1d2df3db89f6": "PENNINE"
    }
    assert {party.method for party in wrong.values()} == {"exact"}

"""`resolve_description` measured against every bank line the dev slice can grade.

Session 2.1 left a question open on purpose: should a structural hit on a short prefix beat a
fuzzy hit on a longer one? This is the measurement that answered it, kept as a test so the next
change to the resolver is made against the same numbers.

The truth is the manifest's: a bank line that settles an invoice belongs to that invoice's
counterparty, and a duplicate belongs to whoever its original does. 881 of the slice's 1,210
lines can be graded that way; the rest are processor payouts, refunds and unknown receipts,
which have no invoice to name a counterparty.

Nothing here touches the alias registry -- it is empty throughout, so what is measured is the
string layers alone, and nothing is learned from ground truth.

| rule                                             | correct | wrong | unresolved |
| ------------------------------------------------ | ------- | ----- | ---------- |
| session 2.1 (structural first, fuzzy last)       | 847     | 22    | 12         |
| 2.1 with only the pinned ordering reversed       | 848     | 22    | 11         |
| session 2.2 (per-prefix, `min(set, ratio)`)      | 836     | 3     | 42         |
"""

from collections import Counter
from pathlib import Path

import pytest

from crossfoot.entities import AliasRegistry, Candidate, NameIndex, resolve_description
from crossfoot.generate.read import read_bank, read_erp, read_manifest
from crossfoot.models.canonical import PartyRef

DEV_SLICE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "dev"

GRADED = 881

#: The three lines no string rule can get right: each description is, character for character,
#: the trading name of a counterparty other than the one that paid.
STILL_WRONG = {
    "bank:2024-04-11:000037": "EASTGATE",
    "bank:2024-08-01:000002": "ROWANBANK INV-2024-0727",
    "bank:2024-08-01:000008": "EASTGATE",
}

Grade = tuple[str, str, PartyRef]


@pytest.fixture(scope="module")
def graded() -> dict[str, Grade]:
    """Every gradable bank line -> (class, verdict, the party it resolved to)."""
    erp = read_erp(DEV_SLICE / "erp.sql")
    index = NameIndex(
        Candidate(counterparty_id=cp.id, legal_name=cp.legal_name, trading_name=cp.trading_name)
        for cp in erp.counterparties
    )
    invoices = erp.invoice_by_ref()
    descriptions = {line.id: line.description for line in read_bank(DEV_SLICE / "bank")}
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
            if not target.startswith("bank:"):
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
    resolved: dict[str, PartyRef] = {}
    out: dict[str, Grade] = {}
    for line_id, payers in truth.items():
        description = descriptions[line_id]
        if description not in resolved:
            resolved[description] = resolve_description(description, index, registry)
        party = resolved[description]
        if party.counterparty_id is None:
            verdict = "unresolved"
        else:
            verdict = "correct" if party.counterparty_id in payers else "wrong"
        out[line_id] = (label[line_id], verdict, party)
    return out


def tally(graded: dict[str, Grade], *classes: str) -> Counter[str]:
    return Counter(
        verdict for kind, verdict, _ in graded.values() if not classes or kind in classes
    )


def test_the_slice_grades_the_lines_the_measurement_claims(graded: dict[str, Grade]) -> None:
    assert len(graded) == GRADED


def test_the_headline_numbers(graded: dict[str, Grade]) -> None:
    assert tally(graded) == {"correct": 836, "wrong": 3, "unresolved": 42}


def test_a_parent_company_name_never_resolves_by_string(graded: dict[str, Grade]) -> None:
    # spec 01 §4.8a builds these to be out of fuzzy reach. Before the per-prefix score, 16 of
    # the 26 resolved -- to an unrelated counterparty that happened to share a first word.
    assert tally(graded, "dba") == {"unresolved": 26}


def test_drifted_names_resolve_or_abstain(graded: dict[str, Grade]) -> None:
    # 16 abstentions: a lone first word, a name cut to where two counterparties share it, or a
    # typo equally near two of them. Abstaining is the answer; guessing was the bug.
    assert tally(graded, "name") == {"correct": 63, "wrong": 3, "unresolved": 16}


def test_every_other_class_resolves_correctly(graded: dict[str, Grade]) -> None:
    others = [grade for grade in graded.values() if grade[0] not in ("name", "dba")]
    assert len(others) == 773
    assert {verdict for _, verdict, _ in others} == {"correct"}
    # And structurally: an untouched or merely truncated name is not a fuzzy match, with or
    # without a reference typed after it.
    assert {party.method for _, _, party in others} == {"exact", "normalised"}


def test_the_wrong_answers_are_exactly_the_ones_no_string_rule_can_fix(
    graded: dict[str, Grade],
) -> None:
    wrong = {
        line_id: party for line_id, (_, verdict, party) in graded.items() if verdict == "wrong"
    }
    assert {line_id: party.surface for line_id, party in wrong.items()} == STILL_WRONG
    # Each is an exact hit on somebody else's name -- not a fuzzy near miss that a better
    # metric would have caught.
    assert {party.method for party in wrong.values()} == {"exact"}


def test_no_fuzzy_match_rests_on_an_inflated_score(graded: dict[str, Grade]) -> None:
    fuzzy = [party for _, _, party in graded.values() if party.method == "fuzzy"]
    assert len(fuzzy) == 31
    # Every one is right, and none reads 100: a subset can no longer pass as a perfect match.
    assert all(verdict == "correct" for _, verdict, p in graded.values() if p.method == "fuzzy")
    assert max(party.similarity for party in fuzzy) < 100

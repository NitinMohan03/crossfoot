"""Each layer of the resolution ladder, in isolation, over the drift spec 01 §4.4 injects.

The scores asserted here are RapidFuzz's, measured, not guessed: a typo pair scores 92, an
`(UK)` suffix scores 100 because the token set is a superset, and a DBA pair scores 24 against its
own subsidiary -- which is exactly why `reasoning(a)` needs the registry and cannot be fuzzed.
"""

import pytest

from crossfoot.config import Thresholds
from crossfoot.entities import AliasRegistry, Candidate, NameIndex, resolve, resolve_description
from crossfoot.names import similarity

ACME = Candidate(counterparty_id=7, legal_name="ACME WIDGETS LTD", trading_name="ACME WIDGETS")
NORTH = Candidate(
    counterparty_id=8, legal_name="NORTHERN LIGHTS TRADING PLC", trading_name="NORTHLIGHT"
)
GOOGLE = Candidate(counterparty_id=9, legal_name="GOOGLE UK LTD", trading_name="GOOGLE UK")


@pytest.fixture
def index() -> NameIndex:
    return NameIndex([ACME, NORTH, GOOGLE])


@pytest.fixture
def empty() -> AliasRegistry:
    return AliasRegistry.empty()


def test_the_exact_name_is_an_exact_match(index: NameIndex, empty: AliasRegistry) -> None:
    party = resolve("ACME WIDGETS LTD", index, empty)
    assert (party.counterparty_id, party.method, party.similarity) == (7, "exact", 100)


def test_a_trading_name_resolves_too(index: NameIndex, empty: AliasRegistry) -> None:
    assert resolve("NORTHLIGHT", index, empty).counterparty_id == 8


def test_case_punctuation_and_a_suffix_swap_are_trivial(
    index: NameIndex, empty: AliasRegistry
) -> None:
    for surface in ("acme widgets, ltd.", "ACME WIDGETS LIMITED", "Acme   Widgets"):
        party = resolve(surface, index, empty)
        assert (party.counterparty_id, party.method) == (7, "normalised"), surface
        assert party.similarity == 100


def test_the_banks_truncation_is_trivial_not_fuzzy(index: NameIndex, empty: AliasRegistry) -> None:
    # spec 01 §4.1: `ACME WIDGETS LT` is still clean; the distinguishing token survives.
    party = resolve("ACME WIDGETS LT", index, empty)
    assert (party.counterparty_id, party.method) == (7, "normalised")


def test_a_typo_lands_in_the_fuzzy_layer(index: NameIndex, empty: AliasRegistry) -> None:
    party = resolve("ACNE WIDGETS LTD", index, empty)
    assert (party.counterparty_id, party.method, party.similarity) == (7, "fuzzy", 92)
    assert party.similarity >= Thresholds.NAME_SIM_MIN


def test_a_uk_suffix_is_fuzzy_at_full_score(index: NameIndex, empty: AliasRegistry) -> None:
    party = resolve("ACME WIDGETS (UK) LTD", index, empty)
    assert (party.counterparty_id, party.method, party.similarity) == (7, "fuzzy", 100)


def test_a_name_nothing_relates_is_unresolved(index: NameIndex, empty: AliasRegistry) -> None:
    party = resolve("QUAYSIDE LOGISTICS", index, empty)
    assert party.counterparty_id is None
    assert party.method == "unresolved"
    # The best score is kept for inspection, and it is below the shared threshold.
    assert party.similarity < Thresholds.NAME_SIM_MIN


def test_a_dba_name_is_unresolved_without_the_registry_and_resolved_with_it(
    index: NameIndex, empty: AliasRegistry
) -> None:
    # spec 01 §4.8a: the payer is the parent, and no string metric relates the two names.
    unresolved = resolve("ALPHABET INC", index, empty)
    assert unresolved.counterparty_id is None
    assert unresolved.similarity < Thresholds.NAME_SIM_MIN
    # Against the subsidiary itself the pair scores 24: not near-miss drift, unrelated strings.
    assert similarity("GOOGLE UK LTD", "ALPHABET INC") == 24

    confirmed = AliasRegistry({"ALPHABET": 9})
    party = resolve("ALPHABET INC", index, confirmed)
    assert (party.counterparty_id, party.method, party.similarity) == (9, "alias", 100)


def test_an_alias_is_looked_up_normalised(index: NameIndex) -> None:
    confirmed = AliasRegistry({"ALPHABET": 9})
    assert resolve("alphabet, inc.", index, confirmed).counterparty_id == 9


def test_a_fuzzy_tie_is_not_an_answer(empty: AliasRegistry) -> None:
    # Both candidates score 100 against "ACME": taking the first would make the result depend
    # on row order, which is the failure plan §2.7 calls out.
    split = NameIndex(
        [
            Candidate(counterparty_id=1, legal_name="ACME NORTH", trading_name="ACME NORTH"),
            Candidate(counterparty_id=2, legal_name="ACME SOUTH", trading_name="ACME SOUTH"),
        ]
    )
    party = resolve("ACME", split, empty)
    assert party.counterparty_id is None
    assert party.similarity == 100


TWINS = NameIndex(
    [
        Candidate(counterparty_id=1, legal_name="ACME WIDGETS LTD", trading_name="ACME ONE"),
        Candidate(counterparty_id=2, legal_name="Acme Widgets PLC", trading_name="ACME TWO"),
    ]
)


def test_two_counterparties_sharing_a_normalised_name_are_ambiguous(
    empty: AliasRegistry,
) -> None:
    assert TWINS.ambiguous == frozenset({"ACME WIDGETS"})
    assert TWINS.lookup_normalised("ACME WIDGETS LTD") is None
    # Neither name normalises to a unique counterparty, and both truncate from the surface, so
    # no deterministic layer may answer -- and the fuzzy layer must not rescue it either.
    party = resolve("ACME WIDGETS", TWINS, empty)
    assert party.counterparty_id is None
    assert party.method == "unresolved"


def test_an_exact_string_still_wins_against_a_normalised_collision(
    empty: AliasRegistry,
) -> None:
    # The two legal names collide only after normalisation. A surface that is one of them
    # character for character names that one counterparty and no other.
    party = resolve("ACME WIDGETS LTD", TWINS, empty)
    assert (party.counterparty_id, party.method) == (1, "exact")


def test_two_counterparties_with_the_same_name_resolve_to_neither(
    empty: AliasRegistry,
) -> None:
    clones = NameIndex(
        [
            Candidate(counterparty_id=1, legal_name="ACME WIDGETS LTD", trading_name="ACME ONE"),
            Candidate(counterparty_id=2, legal_name="ACME WIDGETS LTD", trading_name="ACME TWO"),
        ]
    )
    party = resolve("ACME WIDGETS LTD", clones, empty)
    assert party.counterparty_id is None
    assert party.similarity == 100


def test_an_empty_surface_resolves_to_nothing(index: NameIndex, empty: AliasRegistry) -> None:
    party = resolve("  ...  ", index, empty)
    assert party.counterparty_id is None
    assert party.normalised == ""


def test_a_description_with_a_reference_after_the_name_still_resolves(
    index: NameIndex, empty: AliasRegistry
) -> None:
    party = resolve_description("ACME WIDGETS LT INV-2024-0891", index, empty)
    # The trading name "ACME WIDGETS" is itself a leading run of tokens, so this is exact.
    assert (party.counterparty_id, party.method) == (7, "exact")
    # `surface` stays the whole field: the matcher may still want the part that was not a name.
    assert party.surface == "ACME WIDGETS LT INV-2024-0891"


def test_a_description_resolves_an_exact_name_before_anything_else(
    index: NameIndex, empty: AliasRegistry
) -> None:
    party = resolve_description("ACME WIDGETS LTD 0891", index, empty)
    assert (party.counterparty_id, party.method, party.similarity) == (7, "exact", 100)


def test_a_truncated_name_in_a_description_resolves_trivially(
    index: NameIndex, empty: AliasRegistry
) -> None:
    # No prefix of this field is a counterparty name, so the truncation rule is what resolves
    # it: `NORTHERN LIGHTS TRADIN` is `NORTHERN LIGHTS TRADING PLC` cut at 32 characters.
    party = resolve_description("NORTHERN LIGHTS TRADIN 0891", index, empty)
    assert (party.counterparty_id, party.method) == (8, "normalised")


def test_a_description_can_resolve_through_the_registry(index: NameIndex) -> None:
    confirmed = AliasRegistry({"ALPHABET": 9})
    party = resolve_description("ALPHABET INC 4417", index, confirmed)
    assert (party.counterparty_id, party.method) == (9, "alias")


def test_a_description_of_noise_is_unresolved(index: NameIndex, empty: AliasRegistry) -> None:
    party = resolve_description("FASTER PAYMENT 99321", index, empty)
    assert party.counterparty_id is None


def test_an_index_reports_what_it_holds() -> None:
    index = NameIndex([ACME, NORTH])
    assert len(index) == 2
    assert index.names_for(7) == ("ACME WIDGETS LTD", "ACME WIDGETS")
    assert index.names_for(404) == ()

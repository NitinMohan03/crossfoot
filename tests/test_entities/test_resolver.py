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
    # `ACME WIDGETS LT` is the longest identifying run, and it identifies by truncation, so the
    # method is `normalised` -- not the `exact` hit the shorter `ACME WIDGETS` would have given.
    assert (party.counterparty_id, party.method) == (7, "normalised")
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


def test_a_subset_name_does_not_outscore_the_counterparty_the_payer_meant(
    empty: AliasRegistry,
) -> None:
    """Measured on the generated world: `token_set_ratio` alone resolves this wrongly.

    `STANWICK` (a real trading name in `data/full`) is a token subset of `STANWICK UTILITI3S`,
    so `token_set_ratio` scores it 100, while the typo drops the `STANWICK UTILITIES` the payer
    meant to 94. Ranking on the gate score alone therefore hands the invoice to the wrong
    company -- which is what the live server did before `min(set, sort)` ranked the candidates.
    """
    stanwick = NameIndex(
        [
            Candidate(
                counterparty_id=164,
                legal_name="STANWICK PACKAGING LIMITED",
                trading_name="STANWICK",
            ),
            Candidate(
                counterparty_id=250,
                legal_name="STANWICK UTILITIES & CO",
                trading_name="STANWICK UTILITIES",
            ),
        ]
    )
    # The inflated score is real, and it is the higher of the two.
    assert similarity("STANWICK", "STANWICK UTILITI3S & CO") == 100
    assert similarity("STANWICK UTILITIES", "STANWICK UTILITI3S & CO") == 94

    party = resolve("STANWICK UTILITI3S & CO", stanwick, empty)
    assert party.counterparty_id == 250
    assert party.method == "fuzzy"
    # The reported score stays the shared metric, `token_set_ratio`.
    assert party.similarity == 94


def test_the_longest_prefix_that_identifies_someone_wins(empty: AliasRegistry) -> None:
    stanwick = NameIndex(
        [
            Candidate(counterparty_id=164, legal_name="STANWICK", trading_name="STANWICK"),
            Candidate(
                counterparty_id=250,
                legal_name="STANWICK UTILITIES & CO",
                trading_name="STANWICK UTILITIES",
            ),
        ]
    )
    # `STANWICK UTILITIES` is the longer run of tokens, so it wins even though `STANWICK` alone
    # is also an exact counterparty name.
    assert (
        resolve_description("STANWICK UTILITIES INV-2024-0180", stanwick, empty).counterparty_id
        == 250
    )
    # And on its own, `STANWICK` still resolves to the counterparty of that name.
    assert resolve_description("STANWICK INV-2024-0180", stanwick, empty).counterparty_id == 164


STANWICKS = NameIndex(
    [
        Candidate(counterparty_id=164, legal_name="STANWICK", trading_name="STANWICK"),
        Candidate(
            counterparty_id=250,
            legal_name="STANWICK UTILITIES & CO",
            trading_name="STANWICK UTILITIES",
        ),
    ]
)


def test_a_fuzzy_hit_on_a_long_prefix_beats_a_structural_hit_on_a_short_one() -> None:
    """The limit session 2.1 pinned the other way round, reversed on session 2.2's measurement.

    A typo in the second token leaves `STANWICK` as the only structural hit. Structural used to
    beat fuzzy outright, so this resolved to the single-token counterparty; fuzzy now runs at
    each prefix on the way down, so the longer run of tokens answers first -- which is how a
    human reads it. `tests/test_entities/test_bank_descriptions.py` holds the numbers that
    decided it.
    """
    party = resolve_description(
        "STANWICK UTILITI3S INV-2024-0180", STANWICKS, AliasRegistry.empty()
    )
    assert (party.counterparty_id, party.method, party.similarity) == (250, "fuzzy", 94)


def test_one_shared_leading_word_is_not_a_fuzzy_match(empty: AliasRegistry) -> None:
    """What the reversal needs to be safe: a subset cannot pass the description gate.

    `BRAMBLE` is a subset of `BRAMBLE DESIGN`, so `token_set_ratio` scores the one-token prefix
    100 -- which is how a parent company sharing a first word with an unrelated counterparty
    used to be handed to it. The plain ratio on the same prefix is 67, and the minimum decides.
    """
    bramble = NameIndex(
        [
            Candidate(
                counterparty_id=7, legal_name="BRAMBLE DESIGN LTD", trading_name="BRAMBLE DESIGN"
            )
        ]
    )
    assert similarity("BRAMBLE DESIGN LTD", "BRAMBLE") == 100
    party = resolve_description("BRAMBLE PROPERTIES GROUP PLC", bramble, empty)
    assert party.counterparty_id is None
    assert party.similarity < Thresholds.NAME_SIM_MIN


def test_two_equally_distant_typos_are_a_tie_not_a_guess(empty: AliasRegistry) -> None:
    # `PAZRICK` is two letters from both. Ranking on a sorted-token ratio used to pick GARRICK,
    # because sorting moves `CARRICK` in front of `FABRICATION` and `GARRICK`/`PAZRICK` behind.
    twins = NameIndex(
        [
            Candidate(41, "CARRICK FABRICATION LIMITED", "CARRICK FABRICATION LIMITED"),
            Candidate(58, "GARRICK FABRICATION LLP", "GARRICK FABRICATION LLP"),
        ]
    )
    party = resolve_description("PAZRICK FABRICATION LIMITED", twins, empty)
    assert party.counterparty_id is None
    assert party.similarity >= Thresholds.NAME_SIM_MIN


def test_a_reference_riding_on_an_exact_name_does_not_make_the_match_fuzzy(
    empty: AliasRegistry,
) -> None:
    # The whole field is close enough to the name to pass as fuzzy with the reference attached.
    # That may decide who; it must not downgrade an exact name to a score of 82.
    meridian = NameIndex([Candidate(3, "MERIDIAN AEROSPACE LLP", "MERIDIAN AEROSPACE")])
    party = resolve_description("MERIDIAN AEROSPACE INV0014", meridian, empty)
    assert (party.counterparty_id, party.method, party.similarity) == (3, "exact", 100)


def test_a_description_that_is_another_partys_trading_name_resolves_to_that_party(
    empty: AliasRegistry,
) -> None:
    """What no string rule can fix, pinned so nobody thinks the resolver handles it.

    A payer who truncates `EASTGATE HOLDINGS LTD` to `EASTGATE` has written, character for
    character, the trading name of a different counterparty. The amount and the reference are
    what tell the two apart, and that is the matcher's job.
    """
    eastgate = NameIndex(
        [
            Candidate(11, "EASTGATE TRADING LTD", "EASTGATE"),
            Candidate(12, "EASTGATE HOLDINGS LTD", "EASTGATE HOLDINGS LTD"),
        ]
    )
    party = resolve_description("EASTGATE", eastgate, empty)
    assert (party.counterparty_id, party.method) == (11, "exact")

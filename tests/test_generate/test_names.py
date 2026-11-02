from crossfoot.config import Thresholds
from crossfoot.names import (
    best_similarity,
    normalise,
    normalises_to_any,
    similarity,
    surface_similarity,
    token_prefixes,
    trivially_normalises,
)


def test_normalise_strips_case_punctuation_and_suffixes() -> None:
    assert normalise("Acme Corporation Ltd") == "ACME CORPORATION"
    assert normalise("ACME FOODS & CO") == "ACME FOODS"
    assert normalise("  acme   foods  limited ") == "ACME FOODS"


def test_truncation_is_trivial_only_while_the_name_survives() -> None:
    legal = "ACME CORPORATION LTD"
    assert trivially_normalises(legal, "ACME CORPORATION LT")
    assert trivially_normalises(legal, "acme corporation")
    assert not trivially_normalises(legal, "ACME")


def test_a_different_payer_is_not_trivial() -> None:
    assert not trivially_normalises("ACME FOODS LTD", "ALPHABET INC")
    assert not trivially_normalises("ACME FOODS LTD", "")


def test_similarity_is_an_integer_score() -> None:
    score = similarity("ACME CORPORATION LTD", "ACME CORPORATION PLC")
    assert isinstance(score, int)
    assert score == 100  # normalise strips both suffixes, so a bare swap is no difference


def test_similarity_tracks_the_drift_mechanisms_in_the_spec() -> None:
    """The §4.4 list has to clear NAME_SIM_MIN; the §4.8a case has to fall below it."""
    legal = "ALPHA WIDGETS LTD"
    for drifted in ("ALPHA WIDGETS LTD (UK)", "ALPHA WIDGITS LTD", "ALPHA"):
        assert similarity(legal, drifted) >= Thresholds.NAME_SIM_MIN, drifted
    # A parent name built from a different trade word is out of fuzzy reach.
    assert similarity(legal, "BETA CASTINGS GROUP PLC") < Thresholds.NAME_SIM_MIN
    assert similarity(legal, "ALPHA CASTINGS GROUP PLC") < Thresholds.NAME_SIM_MIN


def test_a_suffix_swap_alone_is_not_a_name_variant() -> None:
    """Which is why `drifted_surface_name` never uses one on its own (spec 01 §4.4)."""
    assert trivially_normalises("ALPHA WIDGETS LTD", "ALPHA WIDGETS PLC")


def test_token_prefixes_recover_the_payer_name_from_a_bank_description() -> None:
    description = "ALPHA WIDGETS LTD INV-2024-0891"
    assert token_prefixes(description)[0] == description
    assert token_prefixes(description)[-1] == "ALPHA"
    # A reference fragment on the end must not cost the name its score.
    assert surface_similarity(("ALPHA WIDGETS LTD",), description) == 100


def test_best_similarity_takes_the_closer_of_legal_and_trading() -> None:
    assert best_similarity(("NORTHERN FOUNDRY SERVICES LTD", "NORFOUND"), "NORFOUND") == 100


def test_normalises_to_any_sees_through_a_reference_fragment() -> None:
    assert normalises_to_any(("ALPHA WIDGETS LTD",), "ALPHA WIDGETS LTD INV-2024-0891")
    assert not normalises_to_any(("ALPHA WIDGETS LTD",), "BETA CASTINGS GROUP PLC")

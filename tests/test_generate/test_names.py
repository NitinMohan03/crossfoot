from crossfoot.names import normalise, trivially_normalises


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

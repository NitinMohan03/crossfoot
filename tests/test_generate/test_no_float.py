"""Money is Decimal or integer minor units, everywhere, with no exceptions.

The Write/Edit hook enforces this for tool edits; this test enforces it for every other route
into the repository, including shell redirects and hand edits.
"""

from pathlib import Path

import crossfoot

NEEDLE = "float" + "("  # assembled so this file does not match itself
# `mock_processor` is deliberately absent: it never parses an amount (records are served as the
# JSON lines the generator wrote), and the only floats in it are the rate limiter's clock seconds.
GUARDED = ("generate", "matching", "blocking", "models", "db")


def test_no_float_constructor_in_money_handling_packages() -> None:
    root = Path(crossfoot.__file__).parent
    offenders = [
        path.relative_to(root).as_posix()
        for package in GUARDED
        for path in sorted((root / package).rglob("*.py"))
        if NEEDLE in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_shared_money_modules_are_clean_too() -> None:
    root = Path(crossfoot.__file__).parent
    for name in ("money.py", "config.py"):
        assert NEEDLE not in (root / name).read_text(encoding="utf-8")

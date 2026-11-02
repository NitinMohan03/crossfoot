"""Remittance advices: the four layouts, the held-out one, and byte stability (spec 01 §6, §9)."""

import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import reportlab

from crossfoot.config import HELD_OUT_TEMPLATE
from crossfoot.generate.emit import remit_pdf
from crossfoot.generate.quotas import NATURAL_RATES
from crossfoot.generate.records import RemitLine, Remittance
from crossfoot.generate.remittance import (
    GLOBAL_TARGET_PCT,
    MANDATORY_SUBTYPES,
    MANDATORY_TYPES,
    TOPPABLE_SUBTYPES,
    TOPPABLE_TYPES,
    top_up_count,
)

LINES = [
    RemitLine("INV-2024-0891", 441700),
    RemitLine("INV-2024-0902", 128350, "rounding adjustment"),
    RemitLine("CN-2024-0007", -5000, "credit note"),
]


def advice(template: str, *, currency: str = "GBP", lines: list[RemitLine] | None = None):  # type: ignore[no-untyped-def]
    rows = LINES if lines is None else lines
    return Remittance(
        id="remit:RA-2024-0088",
        payment_ref="PAY-2024-0088",
        template=template,
        payer="ACME WIDGETS LTD",
        paid_on=date(2024, 3, 14),
        currency=currency,
        total_minor=sum(line.amount_minor for line in rows),
        lines=rows,
        narrative="Payment reduced pending resolution. We hold CN-2025-0031 against INV-2024-0891.",
    )


def test_reportlab_is_pinned() -> None:
    """The committed golden PDFs are bytes, and reportlab writes its version into the header.

    reportlab's `%PDF` banner comment carries a version-specific string that no public API
    overrides, so an upgrade shifts every byte offset in every committed advice. The pin in
    `pyproject.toml` makes that a deliberate regeneration; this test is the tripwire.
    """
    assert reportlab.Version == "5.1.0"


def test_every_layout_renders() -> None:
    for template in ("A", "B", "C", "D"):
        data = remit_pdf.render(advice(template))
        assert data.startswith(b"%PDF-"), template
        assert data.rstrip().endswith(b"%%EOF"), template


def test_an_unknown_layout_is_refused() -> None:
    try:
        remit_pdf.render(advice("Z"))
    except ValueError as error:
        assert "Z" in str(error)
    else:
        raise AssertionError("an unknown template should not render")


def test_bytes_are_identical_across_processes(tmp_path: Path) -> None:
    """Determinism has to survive a fresh interpreter, not just a second call in one."""
    script = tmp_path / "render.py"
    script.write_text(
        "import hashlib\n"
        "from datetime import date\n"
        "from crossfoot.generate.emit import remit_pdf\n"
        "from crossfoot.generate.records import Remittance, RemitLine\n"
        "rows = [RemitLine('INV-2024-0891', 441700)]\n"
        "adv = Remittance(id='x', payment_ref='PAY-2024-0001', template='A',\n"
        "                 payer='ACME LTD', paid_on=date(2024, 3, 14), currency='GBP',\n"
        "                 total_minor=441700, lines=rows, narrative='Paid in full.')\n"
        "print(hashlib.sha256(remit_pdf.render(adv)).hexdigest())\n",
        encoding="utf-8",
    )
    runs = [
        subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, check=True, timeout=120
        ).stdout.strip()
        for _ in range(2)
    ]
    assert runs[0] == runs[1]

    rows = [RemitLine("INV-2024-0891", 441700)]
    inline = Remittance(
        id="x",
        payment_ref="PAY-2024-0001",
        template="A",
        payer="ACME LTD",
        paid_on=date(2024, 3, 14),
        currency="GBP",
        total_minor=441700,
        lines=rows,
        narrative="Paid in full.",
    )
    assert hashlib.sha256(remit_pdf.render(inline)).hexdigest() == runs[0]


def test_no_wall_clock_leaks_into_the_bytes() -> None:
    data = remit_pdf.render(advice("A"))
    # Invariant mode does not drop /ID; it derives it from a digest of the content, so the
    # same input gives the same value. Stability is what matters, not absence.
    assert data.count(b"/ID") == 1
    assert b"D:20000101000000" in data
    assert remit_pdf.render(advice("A")) == data
    # Our own metadata, not reportlab's version-dependent defaults.
    assert remit_pdf.PRODUCER.encode() in data
    assert remit_pdf.CREATOR.encode() in data
    assert b"www.reportlab.com" not in data.split(b"\n", 2)[2]


def test_layout_c_paginates_past_its_row_cap() -> None:
    few = remit_pdf.render(advice("C"))
    many = remit_pdf.render(
        advice("C", lines=[RemitLine(f"INV-2024-{n:04d}", 10_000 + n) for n in range(30)])
    )
    assert b"/Count 1" in few
    assert b"/Count 2" in many
    assert b"continued overleaf" in many


def test_layout_d_is_landscape_and_writes_its_own_ref_style() -> None:
    portrait = remit_pdf.render(advice("A"))
    landscape = remit_pdf.render(advice("D"))
    # A4 is 595x842pt; landscape swaps them.
    assert b"/MediaBox [ 0 0 595.2756 841.8898 ]" in portrait
    assert b"/MediaBox [ 0 0 841.8898 595.2756 ]" in landscape
    assert remit_pdf.slashed_ref(RemitLine("INV-2024-0891", 1)) == "0891/2024"
    assert remit_pdf.slashed_ref(RemitLine("CN-2024-0007", 1)) == "CN 0007/2024"


def test_every_currency_symbol_renders() -> None:
    for currency in ("GBP", "USD", "EUR"):
        data = remit_pdf.render(advice("C", currency=currency))
        assert data.startswith(b"%PDF-"), currency
    assert remit_pdf.money(123456789, currency="GBP", grouped=True) == "£1,234,567.89"
    assert remit_pdf.money(-5000, currency="EUR") == "-€50.00"


def test_the_top_up_rate_brings_the_natural_rates_to_the_global_target() -> None:
    """§6.4's arithmetic, checked against the rate table it was derived from."""
    mandatory = sum(NATURAL_RATES[name] for name in MANDATORY_TYPES)
    mandatory += NATURAL_RATES["reasoning"] * len(MANDATORY_SUBTYPES) // 3
    toppable = sum(NATURAL_RATES[name] for name in TOPPABLE_TYPES)
    toppable += NATURAL_RATES["reasoning"] * len(TOPPABLE_SUBTYPES) // 3
    assert mandatory == 11
    assert toppable == 86

    # At 10,000 events the top-ups land exactly on the target.
    per_type = {
        "clean": 5700,
        "fee": 1000,
        "timing": 800,
        "name": 700,
        "fx": 300,
        "reasoning/dba": 100,
    }
    topped = sum(top_up_count(count) for count in per_type.values())
    assert topped + 1100 == GLOBAL_TARGET_PCT * 100


def test_remit_jsonl_records_the_held_out_template(dataset_dir: Path) -> None:
    rows = [
        json.loads(line)
        for line in (dataset_dir / "remit" / "remit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows, "the fixture has no remittance advices"
    templates = {row["template"] for row in rows}
    assert HELD_OUT_TEMPLATE in templates
    # One PDF per advice, named by payment_ref, and nothing else in the directory.
    on_disk = {path.stem for path in (dataset_dir / "remit").glob("*.pdf")}
    assert on_disk == {row["payment_ref"] for row in rows}
    # The template id is in the sidecar, never on the PDF itself (spec 01 §6).
    sample = next(row for row in rows if row["template"] == HELD_OUT_TEMPLATE)
    pdf = (dataset_dir / "remit" / f"{sample['payment_ref']}.pdf").read_bytes()
    assert b"template" not in pdf.lower()

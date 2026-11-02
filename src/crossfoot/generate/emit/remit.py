"""Remittance advices: one PDF per payment plus the structured truth (spec 01 §3.4).

`remit.jsonl` records what each advice says, **including** the held-out template D, so parse
rate can be measured against it in Phase 2 without ever opening a D file during development.
The template id lives here and not on the PDF.
"""

import json
from collections.abc import Sequence
from pathlib import Path

from crossfoot.generate.emit.remit_pdf import render
from crossfoot.generate.records import Event, Remittance


def payload(advice: Remittance) -> dict[str, object]:
    return {
        "id": advice.id,
        "lines": [
            {
                "amount_minor": line.amount_minor,
                "invoice_ref": line.invoice_ref,
                "note": line.note,
            }
            for line in advice.lines
        ],
        "narrative": advice.narrative,
        "paid_on": advice.paid_on.isoformat(),
        "payer": advice.payer,
        "payment_ref": advice.payment_ref,
        "template": advice.template,
        "total_minor": advice.total_minor,
    }


def advices(events: Sequence[Event]) -> list[Remittance]:
    return sorted(
        (event.remittance for event in events if event.remittance is not None),
        key=lambda advice: advice.id,
    )


def write(out_dir: Path, events: Sequence[Event]) -> None:
    out = out_dir / "remit"
    out.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for advice in advices(events):
        (out / f"{advice.payment_ref}.pdf").write_bytes(render(advice))
        lines.append(json.dumps(payload(advice), sort_keys=True, separators=(",", ":")))

    body = "\n".join(lines)
    (out / "remit.jsonl").write_text(body + "\n" if body else "", encoding="utf-8", newline="\n")

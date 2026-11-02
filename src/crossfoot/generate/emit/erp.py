"""The ERP dump: plain SQL, loaded into Postgres by `just db-load` (spec 01 §3.1).

The parent/DBA registry is deliberately absent: the matcher must not be able to read it.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from crossfoot.generate.records import Event
from crossfoot.generate.world import World

BATCH: Final = 500

DDL: Final = """BEGIN;

DROP SCHEMA IF EXISTS erp CASCADE;
CREATE SCHEMA erp;

CREATE TABLE erp.counterparties (
    id             integer PRIMARY KEY,
    legal_name     text NOT NULL,
    trading_name   text NOT NULL,
    country        text NOT NULL,
    currency       text NOT NULL,
    payment_method text NOT NULL,
    terms_days     integer NOT NULL,
    created_at     timestamptz NOT NULL
);

CREATE TABLE erp.invoices (
    ref               text PRIMARY KEY,
    counterparty_id   integer NOT NULL REFERENCES erp.counterparties(id),
    issue_date        date NOT NULL,
    due_date          date NOT NULL,
    currency          text NOT NULL,
    amount_minor      bigint NOT NULL,
    base_amount_minor bigint NOT NULL,
    fx_rate           numeric(12,6),
    fx_rate_date      date,
    created_at        timestamptz NOT NULL
);

CREATE TABLE erp.credit_notes (
    ref               text PRIMARY KEY,
    counterparty_id   integer NOT NULL REFERENCES erp.counterparties(id),
    issued_on         date NOT NULL,
    currency          text NOT NULL,
    amount_minor      bigint NOT NULL,
    base_amount_minor bigint NOT NULL,
    created_at        timestamptz NOT NULL
);
"""


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def stamp(moment: datetime) -> str:
    return quote(moment.strftime("%Y-%m-%d %H:%M:%S+00"))


def _insert(table: str, columns: Sequence[str], rows: Sequence[str]) -> list[str]:
    out: list[str] = []
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        out.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES")
        out.append(",\n".join(f"  ({row})" for row in chunk) + ";")
    return out


def render(world: World, events: Sequence[Event]) -> str:
    lines = [f"-- crossfoot ERP dump: seed={world.seed} kind={world.kind}", "", DDL]

    cp_rows = [
        ", ".join(
            (
                str(cp.id),
                quote(cp.legal_name),
                quote(cp.trading_name),
                quote(cp.country),
                quote(cp.currency),
                quote(cp.payment_method),
                str(cp.terms_days),
                stamp(cp.created_at),
            )
        )
        for cp in sorted(world.counterparties, key=lambda cp: cp.id)
    ]
    lines += _insert(
        "erp.counterparties",
        (
            "id",
            "legal_name",
            "trading_name",
            "country",
            "currency",
            "payment_method",
            "terms_days",
            "created_at",
        ),
        cp_rows,
    )

    invoices = sorted(
        (invoice for event in events for invoice in event.invoices), key=lambda item: item.ref
    )
    invoice_rows = [
        ", ".join(
            (
                quote(invoice.ref),
                str(invoice.counterparty_id),
                quote(invoice.issue_date.isoformat()),
                quote(invoice.due_date.isoformat()),
                quote(invoice.currency),
                str(invoice.amount_minor),
                str(invoice.base_amount_minor),
                "NULL" if invoice.fx_rate_ppm is None else _rate(invoice.fx_rate_ppm),
                "NULL" if invoice.fx_rate_date is None else quote(invoice.fx_rate_date.isoformat()),
                stamp(invoice.created_at),
            )
        )
        for invoice in invoices
    ]
    lines += _insert(
        "erp.invoices",
        (
            "ref",
            "counterparty_id",
            "issue_date",
            "due_date",
            "currency",
            "amount_minor",
            "base_amount_minor",
            "fx_rate",
            "fx_rate_date",
            "created_at",
        ),
        invoice_rows,
    )

    notes = sorted(
        (note for event in events for note in event.credit_notes), key=lambda item: item.ref
    )
    note_rows = [
        ", ".join(
            (
                quote(note.ref),
                str(note.counterparty_id),
                quote(note.issued_on.isoformat()),
                quote(note.currency),
                str(note.amount_minor),
                str(note.base_amount_minor),
                stamp(note.created_at),
            )
        )
        for note in notes
    ]
    if note_rows:
        lines += _insert(
            "erp.credit_notes",
            (
                "ref",
                "counterparty_id",
                "issued_on",
                "currency",
                "amount_minor",
                "base_amount_minor",
                "created_at",
            ),
            note_rows,
        )

    lines += ["", "COMMIT;"]
    return "\n".join(lines) + "\n"


def _rate(rate_ppm: int) -> str:
    whole, part = divmod(rate_ppm, 1_000_000)
    return f"{whole}.{part:06d}"


def write(out_dir: Path, world: World, events: Sequence[Event]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "erp.sql").write_text(render(world, events), encoding="utf-8", newline="\n")

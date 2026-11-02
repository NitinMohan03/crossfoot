"""Readers for a generated dataset directory.

The validator reads what the sources actually contain -- it re-parses `erp.sql` and the bank
CSVs rather than trusting an in-memory copy, so a writer bug cannot hide behind itself.
"""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

BANK_HEADER = "Transaction Date,Transaction Type,Description,Paid Out,Paid In,Balance"


@dataclass(frozen=True, slots=True)
class ErpCounterparty:
    id: int
    legal_name: str
    trading_name: str
    country: str
    currency: str
    payment_method: str
    terms_days: int


@dataclass(frozen=True, slots=True)
class ErpInvoice:
    ref: str
    counterparty_id: int
    issue_date: date
    due_date: date
    currency: str
    amount_minor: int
    base_amount_minor: int
    fx_rate: str | None
    fx_rate_date: date | None

    @property
    def record_id(self) -> str:
        return f"erp:{self.ref}"


@dataclass(frozen=True, slots=True)
class ErpCreditNote:
    ref: str
    counterparty_id: int
    issued_on: date
    currency: str
    amount_minor: int
    base_amount_minor: int

    @property
    def record_id(self) -> str:
        return f"erp:{self.ref}"


@dataclass(frozen=True, slots=True)
class BankLine:
    id: str
    value_date: date
    txn_type: str
    description: str
    paid_out_minor: int
    paid_in_minor: int
    balance_minor: int
    month: str
    row: int


@dataclass(frozen=True, slots=True)
class Erp:
    counterparties: tuple[ErpCounterparty, ...]
    invoices: tuple[ErpInvoice, ...]
    credit_notes: tuple[ErpCreditNote, ...]

    def invoice_by_ref(self) -> dict[str, ErpInvoice]:
        return {invoice.ref: invoice for invoice in self.invoices}

    def counterparty_by_id(self) -> dict[int, ErpCounterparty]:
        return {cp.id: cp for cp in self.counterparties}


def parse_minor(text: str, places: int = 2) -> int:
    """`"4417.00"` -> `441700`, without ever constructing a float."""
    cleaned = text.strip()
    if not cleaned:
        return 0
    sign = -1 if cleaned.startswith("-") else 1
    cleaned = cleaned.lstrip("+-")
    whole, _, frac = cleaned.partition(".")
    frac = (frac + "0" * places)[:places]
    value: int = int(whole or "0") * 10**places + int(frac or "0")
    return sign * value


def read_text_raw(path: Path) -> str:
    """Read a file without newline translation, so CRLF stays CRLF."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def split_values(row: str) -> list[str]:
    """Split a SQL VALUES tuple on commas that are outside quotes."""
    out: list[str] = []
    buffer: list[str] = []
    in_quote = False
    index = 0
    while index < len(row):
        char = row[index]
        if char == "'":
            if in_quote and index + 1 < len(row) and row[index + 1] == "'":
                buffer.append("'")
                index += 2
                continue
            in_quote = not in_quote
            index += 1
            continue
        if char == "," and not in_quote:
            out.append("".join(buffer).strip())
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    out.append("".join(buffer).strip())
    return out


def _maybe_date(value: str) -> date | None:
    return None if value == "NULL" else date.fromisoformat(value)


def read_erp(path: Path) -> Erp:
    counterparties: list[ErpCounterparty] = []
    invoices: list[ErpInvoice] = []
    credit_notes: list[ErpCreditNote] = []
    table = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("INSERT INTO "):
            table = line.split()[2]
            continue
        if not line.startswith("(") or not table:
            continue
        values = split_values(line.rstrip(",;").removeprefix("(").removesuffix(")"))
        if table == "erp.counterparties":
            counterparties.append(
                ErpCounterparty(
                    id=int(values[0]),
                    legal_name=values[1],
                    trading_name=values[2],
                    country=values[3],
                    currency=values[4],
                    payment_method=values[5],
                    terms_days=int(values[6]),
                )
            )
        elif table == "erp.invoices":
            invoices.append(
                ErpInvoice(
                    ref=values[0],
                    counterparty_id=int(values[1]),
                    issue_date=date.fromisoformat(values[2]),
                    due_date=date.fromisoformat(values[3]),
                    currency=values[4],
                    amount_minor=int(values[5]),
                    base_amount_minor=int(values[6]),
                    fx_rate=None if values[7] == "NULL" else values[7],
                    fx_rate_date=_maybe_date(values[8]),
                )
            )
        elif table == "erp.credit_notes":
            credit_notes.append(
                ErpCreditNote(
                    ref=values[0],
                    counterparty_id=int(values[1]),
                    issued_on=date.fromisoformat(values[2]),
                    currency=values[3],
                    amount_minor=int(values[4]),
                    base_amount_minor=int(values[5]),
                )
            )
    return Erp(tuple(counterparties), tuple(invoices), tuple(credit_notes))


def read_bank(directory: Path) -> list[BankLine]:
    """Bank lines with their ids reconstructed from file and row order (spec 01 §3.2)."""
    lines: list[BankLine] = []
    for csv_path in sorted(directory.glob("*.csv")):
        raw = read_text_raw(csv_path)
        rows = raw.split("\r\n")
        if rows and rows[-1] == "":
            rows.pop()
        if not rows or rows[0] != BANK_HEADER:
            raise ValueError(f"{csv_path.name}: unexpected header")
        for row_number, row in enumerate(rows[1:], 1):
            fields = row.split(",")
            if len(fields) != 6:
                raise ValueError(f"{csv_path.name} row {row_number}: expected 6 fields")
            day, txn_type, description, paid_out, paid_in, balance = fields
            value_date = date(int(day[6:10]), int(day[3:5]), int(day[0:2]))
            lines.append(
                BankLine(
                    id=f"bank:{value_date.isoformat()}:{row_number:06d}",
                    value_date=value_date,
                    txn_type=txn_type,
                    description=description,
                    paid_out_minor=parse_minor(paid_out),
                    paid_in_minor=parse_minor(paid_in),
                    balance_minor=parse_minor(balance),
                    month=csv_path.stem,
                    row=row_number,
                )
            )
    return lines


def read_fx(path: Path) -> dict[date, dict[str, int]]:
    """`fx_rates.csv` back into parts per million, so the validator can redo the conversions.

    Parsed as integers, never through a float: `1.270000` -> `1_270_000` ppm (spec 01 §3.5).
    """
    rows = path.read_text(encoding="utf-8").splitlines()
    if not rows:
        raise ValueError(f"{path} is empty")
    codes = rows[0].split(",")[1:]
    table: dict[date, dict[str, int]] = {}
    for row in rows[1:]:
        if not row:
            continue
        fields = row.split(",")
        table[date.fromisoformat(fields[0])] = {
            code: parse_minor(value, 6) for code, value in zip(codes, fields[1:], strict=True)
        }
    return table


def read_remit(directory: Path) -> list[dict[str, Any]]:
    return read_jsonl(directory / "remit.jsonl")


def pdf_names(directory: Path) -> set[str]:
    """The `<payment_ref>` of every PDF actually on disk."""
    return {path.stem for path in directory.glob("*.pdf")}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def read_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = read_jsonl(path)
    if not rows:
        raise ValueError(f"{path} is empty")
    return rows[0], rows[1:]

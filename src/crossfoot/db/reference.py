"""Load the FX rate and bank-holiday tables from a generated dataset directory.

The CSVs are the generator's output (spec 01 §3.5): `fx_rates.csv` is wide (`date,USD,EUR`) and
is stored long, one row per (date, currency). Rates convert to integer parts per million, the
same representation `crossfoot.generate.fx` uses, and the conversion refuses to round: a rate
with a seventh decimal place is a bug in the generator, not something to silently absorb.
"""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import psycopg

from crossfoot.db.schema import REF_SQL, sql

FX_FILE: Final = "fx_rates.csv"
HOLIDAY_FILE: Final = "holidays_gb_eng.csv"
REGION: Final = "GB-ENG"
PPM: Final = 1_000_000

FxRow = tuple[date, str, int]
HolidayRow = tuple[date, str, str]


@dataclass(frozen=True, slots=True)
class LoadCounts:
    fx_rates: int
    bank_holidays: int


def ddl() -> str:
    """The `ref` schema DDL, shipped as package data."""
    return sql(REF_SQL)


def rate_ppm(raw: str) -> int:
    """`"1.270000"` -> `1270000`, exactly or not at all."""
    scaled = Decimal(raw) * PPM
    if scaled != scaled.to_integral_value():
        raise ValueError(f"fx rate {raw!r} has more than 6 decimal places")
    return int(scaled)


def read_fx_rows(path: Path) -> list[FxRow]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        if not fields or fields[0] != "date" or len(fields) < 2:
            raise ValueError(f"{path}: expected a `date` column followed by currency columns")
        currencies = fields[1:]
        return [
            (date.fromisoformat(line["date"]), currency, rate_ppm(line[currency]))
            for line in reader
            for currency in currencies
        ]


def read_holiday_rows(path: Path) -> list[HolidayRow]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{path}: no holidays")
    return [(date.fromisoformat(row["date"]), row["name"], REGION) for row in rows]


def load_reference(dataset_dir: Path, conn: psycopg.Connection[Any]) -> LoadCounts:
    """Create the `ref` schema if needed and replace both tables, in one transaction.

    Replace rather than upsert: these tables are a projection of two files, so the files are
    the truth and a re-run should leave no trace of the previous load.
    """
    missing = [name for name in (FX_FILE, HOLIDAY_FILE) if not (dataset_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"{dataset_dir} is missing {', '.join(missing)} -- run `just seed 42` first"
        )
    fx = read_fx_rows(dataset_dir / FX_FILE)
    holidays = read_holiday_rows(dataset_dir / HOLIDAY_FILE)

    with conn.cursor() as cur:
        cur.execute(ddl())
        cur.execute("TRUNCATE ref.fx_rates, ref.bank_holidays")
        cur.executemany(
            "INSERT INTO ref.fx_rates (rate_date, quote_currency, rate_ppm) VALUES (%s, %s, %s)",
            fx,
        )
        cur.executemany(
            "INSERT INTO ref.bank_holidays (holiday_date, name, region) VALUES (%s, %s, %s)",
            holidays,
        )
    conn.commit()
    return LoadCounts(fx_rates=len(fx), bank_holidays=len(holidays))

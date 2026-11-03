"""The record store: keyset pagination over the Phase 1 settlement JSONL, served verbatim.

Each record keeps its **original JSON line**. That is deliberate on two counts:

* `settlements.jsonl` carries `fx_rate` as a string and every amount as integer minor units
  (spec 01 §3.3). Re-serialising would mean parsing numbers, and the only number type
  `json.dumps` round-trips through is float. Serving the stored line means no amount is ever
  parsed at all, so the no-float rule holds by construction rather than by care.
* "paginating the full settlement set returns exactly the generated records" (plan §4.4) becomes
  a byte-level assertion instead of a semantic one.

Only the fields needed to order and filter are parsed: the id and the one date each collection
sorts on. `(sort_date, id)` is unique, so keyset pagination can neither skip nor duplicate a row.
"""

import json
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

SETTLEMENTS_FILE: Final = "settlements.jsonl"
BATCHES_FILE: Final = "batches.jsonl"
# The date each collection is ordered by. Settlements by when the money settled, batches by when
# the batch closed -- both are the field a client would page through chronologically.
SETTLEMENT_DATE_FIELD: Final = "settled_on"
BATCH_DATE_FIELD: Final = "closed_on"

Key = tuple[date, str]


@dataclass(frozen=True, slots=True)
class Record:
    """One line of the source file, with just enough parsed to order it."""

    sort_date: date
    id: str
    raw: str


class Collection:
    """An ordered, immutable set of records with keyset pagination."""

    def __init__(self, records: Iterable[Record]) -> None:
        self._records: tuple[Record, ...] = tuple(
            sorted(records, key=lambda record: (record.sort_date, record.id))
        )
        self._keys: tuple[Key, ...] = tuple((r.sort_date, r.id) for r in self._records)
        self._by_id: dict[str, Record] = {r.id: r for r in self._records}
        if len(self._by_id) != len(self._records):
            raise ValueError("duplicate record id in source file")

    def __len__(self) -> int:
        return len(self._records)

    def get(self, record_id: str) -> Record | None:
        return self._by_id.get(record_id)

    def page(
        self,
        *,
        after: Key | None,
        limit: int,
        lo: date | None = None,
        hi: date | None = None,
    ) -> tuple[tuple[Record, ...], Key | None]:
        """Up to `limit` records after `after`, within `[lo, hi]`, plus the next page's key.

        The key is `None` when this page is the last one, which is how the caller knows to stop.
        """
        start = 0
        if lo is not None:
            start = bisect_left(self._keys, (lo, ""))
        if after is not None:
            start = max(start, bisect_right(self._keys, after))

        stop = start
        out: list[Record] = []
        while stop < len(self._records) and len(out) < limit:
            record = self._records[stop]
            if hi is not None and record.sort_date > hi:
                break
            out.append(record)
            stop += 1

        next_key: Key | None = None
        if out and stop < len(self._records):
            following = self._records[stop]
            if hi is None or following.sort_date <= hi:
                next_key = (out[-1].sort_date, out[-1].id)
        return tuple(out), next_key


@dataclass(frozen=True, slots=True)
class Store:
    settlements: Collection
    batches: Collection


def read_collection(path: Path, date_field: str) -> Collection:
    """Parse a JSONL file into a `Collection`, keeping every line as written."""
    records: list[Record] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = line.strip()
        if not raw:
            continue
        # parse_float=Decimal so that even a field we discard can never become a float.
        parsed = json.loads(raw, parse_float=Decimal)
        try:
            record_id = parsed["id"]
            sort_date = date.fromisoformat(parsed[date_field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{number}: cannot read id/{date_field}") from exc
        records.append(Record(sort_date=sort_date, id=str(record_id), raw=raw))
    return Collection(records)


def load_store(data_dir: Path) -> Store:
    """Load `<data_dir>/processor/{settlements,batches}.jsonl`."""
    processor = data_dir / "processor"
    missing = [
        name for name in (SETTLEMENTS_FILE, BATCHES_FILE) if not (processor / name).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"{processor} is missing {', '.join(missing)} -- run `just seed 42` first"
        )
    return Store(
        settlements=read_collection(processor / SETTLEMENTS_FILE, SETTLEMENT_DATE_FIELD),
        batches=read_collection(processor / BATCHES_FILE, BATCH_DATE_FIELD),
    )


def body(records: Sequence[Record], next_cursor: str | None) -> str:
    """Assemble a list response from the stored lines, so records go out as generated."""
    data = ",".join(record.raw for record in records)
    return (
        f'{{"data":[{data}],'
        f'"next_cursor":{json.dumps(next_cursor)},'
        f'"has_more":{json.dumps(next_cursor is not None)}}}'
    )

"""ERP rows to canonical records. Rule 3: the only place that knows how the ERP spells things.

Two ERP-specific decisions live here and nowhere else:

* **A credit note is negative.** The dump stores credit notes as positive amounts with their own
  ref (spec 01 §3.1); the canonical sign convention is "positive is owed to us", and the
  manifest treats a credit note as a negative contribution to a consolidated group. Negating
  once, here, is what keeps every later sum honest.
* **The ERP states its own party.** Every row carries `counterparty_id`, so there is nothing to
  resolve: the party is `exact` with the id the ledger gave. Entity resolution is for the
  *payment* side, where a name is all there is.
"""

from collections.abc import Mapping
from datetime import UTC
from typing import Any

from crossfoot.models.canonical import CanonicalRecord, ErpDetail, Money, PartyRef
from crossfoot.names import normalise

#: The ERP has no payment-status column. "Open" is not a field here -- see `tools.list_invoices`.
SOURCE = "erp"


def _party(row: Mapping[str, Any]) -> PartyRef:
    legal = str(row["legal_name"])
    return PartyRef(
        surface=legal,
        normalised=normalise(legal),
        counterparty_id=int(row["counterparty_id"]),
        method="exact",
        similarity=100,
    )


def _money(row: Mapping[str, Any], sign: int) -> Money:
    rate_ppm = row.get("fx_rate_ppm")
    return Money(
        minor=sign * int(row["amount_minor"]),
        currency=str(row["currency"]),
        base_minor=sign * int(row["base_amount_minor"]),
        fx_rate_ppm=None if rate_ppm is None else int(rate_ppm),
        fx_rate_date=row.get("fx_rate_date"),
    )


def _created(row: Mapping[str, Any]) -> tuple[Any, str | None]:
    created = row.get("created_at")
    if created is None:
        return None, None
    moment = created.astimezone(UTC)
    # The dump wrote a timestamptz; its own rendering, offset included, is the original text.
    return moment, created.isoformat()


def invoice_record(row: Mapping[str, Any]) -> CanonicalRecord:
    event_time, event_time_source = _created(row)
    return CanonicalRecord(
        record_id=f"{SOURCE}:{row['ref']}",
        source="erp",
        role="obligation",
        amount=_money(row, 1),
        party=_party(row),
        record_date=row["issue_date"],
        due_date=row["due_date"],
        event_time=event_time,
        event_time_source=event_time_source,
        reference=str(row["ref"]),
        detail=ErpDetail(
            document="invoice",
            counterparty_id=int(row["counterparty_id"]),
            terms_days=None if row.get("terms_days") is None else int(row["terms_days"]),
        ),
    )


def credit_note_record(row: Mapping[str, Any]) -> CanonicalRecord:
    event_time, event_time_source = _created(row)
    return CanonicalRecord(
        record_id=f"{SOURCE}:{row['ref']}",
        source="erp",
        role="obligation",
        amount=_money(row, -1),
        party=_party(row),
        record_date=row["issued_on"],
        reference=str(row["ref"]),
        event_time=event_time,
        event_time_source=event_time_source,
        detail=ErpDetail(
            document="credit_note",
            counterparty_id=int(row["counterparty_id"]),
            terms_days=None if row.get("terms_days") is None else int(row["terms_days"]),
        ),
    )

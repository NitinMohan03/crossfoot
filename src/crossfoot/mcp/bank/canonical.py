"""Bank rows to canonical records. Rule 3: the only place that knows how the bank spells things.

Four bank-specific decisions live here and nowhere else:

* **Sign.** `Paid In` is positive and `Paid Out` negative -- the canonical "money in" convention
  -- so an `FPO` refund reduces what a sum of receipts adds up to without anyone remembering to
  subtract it. The account is sterling, so `base_minor` is the same number.
* **The value date stays a date.** `Transaction Date` is a calendar date with no timezone and is
  copied into `record_date` untouched. The posting *moment* comes from the sidecar and is the
  only thing converted: `event_time` is UTC, `event_time_source` the local text it came from.
  The two can disagree about the day -- a line posted at `2024-04-02T00:02:00+01:00` happened at
  23:02 UTC on the 1st and still has a value date of the 2nd. Both are right.
* **A processor payout names no counterparty.** A `TFR` line is the processor paying out a
  batch (spec 01 §3.2); its description is a batch id, not a payer. It is not put through
  entity resolution at all, because a resolver given `PAYCO SETTLE BAT_...` can only be wrong.
* **The typed reference is the last token, if it has a digit in it.** The description is one
  field holding a payer name and, about half the time, whatever the payer typed: `INV-2024-0577`,
  `INV0517`, `0600`, their own `PO88766`. Every form is one token carrying a digit, and a name
  token never does. It goes to `reference_surface` as written. `reference` stays empty: the
  export has no structured reference column, and pretending a fragment is one would be a lie
  the matcher could not see through.
"""

from collections.abc import Callable

from crossfoot.mcp.bank.parse import BankRow, PostedAt
from crossfoot.models.canonical import BASE_CURRENCY, BankDetail, CanonicalRecord, Money, PartyRef
from crossfoot.names import normalise

SOURCE = "bank"
#: The one transaction type whose description is not a payer (spec 01 §3.2).
PROCESSOR_PAYOUT = "TFR"

Resolver = Callable[[str], PartyRef]


def reference_fragment(description: str) -> str | None:
    """The reference the payer typed after the name, or None when there is only a name."""
    tokens = description.split(" ")
    if len(tokens) < 2:
        return None
    last = tokens[-1]
    return last if any(char.isdigit() for char in last) else None


def unresolved(description: str) -> PartyRef:
    """A party nobody tried to identify: the surface text, and no counterparty."""
    return PartyRef(surface=description, normalised=normalise(description))


def party_for(row: BankRow, resolver: Resolver) -> PartyRef:
    """Who a line is from or to. `resolver` is `crossfoot.entities.resolve_description`."""
    if row.txn_type == PROCESSOR_PAYOUT:
        return unresolved(row.description)
    return resolver(row.description)


def bank_record(row: BankRow, party: PartyRef, posted: PostedAt | None) -> CanonicalRecord:
    """One statement line in the canonical shape. `posted` is None when the sidecar lacks it."""
    return CanonicalRecord(
        record_id=row.line_id,
        source="bank",
        role="payment",
        amount=Money(minor=row.amount_minor, currency=BASE_CURRENCY, base_minor=row.amount_minor),
        party=party,
        record_date=row.value_date,
        event_time=None if posted is None else posted.moment,
        event_time_source=None if posted is None else posted.source,
        reference_surface=reference_fragment(row.description),
        free_text=row.description,
        detail=BankDetail(txn_type=row.txn_type, balance_minor=row.balance_minor),
        raw=row.raw,
    )

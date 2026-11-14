"""Advices to canonical records. Rule 3: the only place that knows how this source spells things.

Five remittance-specific decisions live here and nowhere else:

* **The record id is derived from the payment reference.** An advice prints one identifier,
  the payment it advises on (`PAY-2024-0088`), and no id of its own. Its record id is the
  manifest's `remit:RA-2024-0088` (spec 01 §5.3): the same year and number, because the
  generator numbers both in payment order. That rule is this dataset's and is written down
  here once; `tests/test_mcp/remittance/test_canonical.py` holds it against every advice the
  ground truth lists. It is not read from `remit.jsonl`, which is never an input.
* **The amount is the stated total, and the lines are what the payer wrote.** `amount.minor` is
  the total printed on the advice; each `AdviceLine` is a line as printed. Nothing is compared
  with the ERP. An advice can differ from the ledger by pence on purpose (spec 01 §4.8, the
  `rounding` class), and a short payment's line is the short amount: to "correct" either here
  would remove the one fact the matcher needs the advice for.
* **The currency is a symbol.** `£`, `$` and `€` are sterling, US dollars and euros -- the three
  currencies this business invoices in (spec 01 §4.7). The parser has already refused an advice
  that shows two symbols or none.
* **The sterling view of a foreign advice is an inference, and says so.** An advice in euros
  states no rate. `base_minor` is its total at the rate for its own payment date, from the
  dataset's `fx_rates.csv`, and the record carries that rate and that date so nobody mistakes
  it for something the payer wrote. The rate comes from the file beside the advices rather
  than from `ref.fx_rates`: that table is a projection of one dataset's file and holds
  whichever was loaded last, so an advice from one dataset could be valued at another's rates
  with nothing to show for it.
* **`paid_on` stays a date.** It is the payer's calendar date, copied into `record_date`
  untouched. An advice records no moment, so there is no `event_time` to convert.

The payment reference goes to `reference`: the advice has a field for it. The paragraph goes to
`free_text` and to `detail.narrative`, whole. It is not searched for names or amounts -- an
advice that names a subsidiary only in its paragraph (spec 01 §4.8a) leaves its `party`
unresolved, and reading that paragraph is the adjudicating model's work, not a mapper's.

The party is resolved by the caller through `crossfoot.entities` and handed in; nothing here
compares names. `raw` is empty: a PDF has no one line of text that is the record.
"""

import re
from typing import Final

from crossfoot.mcp.processor.canonical import to_base_minor
from crossfoot.mcp.remittance.files import PAYMENT_REF
from crossfoot.mcp.remittance.parse import Advice
from crossfoot.models.canonical import (
    BASE_CURRENCY,
    AdviceLine,
    CanonicalRecord,
    Money,
    PartyRef,
    RemittanceDetail,
)

SOURCE: Final = "remittance"
#: How this source writes a currency. Checked against `parse.SYMBOLS` in the tests.
CURRENCY_OF: Final = {"£": "GBP", "$": "USD", "€": "EUR"}
#: `remit:RA-2024-0088`, with or without the source prefix (spec 01 §5.3).
REMITTANCE_ID: Final = re.compile(r"^(?:remit:)?RA-(\d{4})-(\d{4,})$")


def remittance_id(payment_ref: str) -> str:
    """`PAY-2024-0088` -> `remit:RA-2024-0088`."""
    match = PAYMENT_REF.match(payment_ref)
    if match is None:
        raise ValueError(f"{payment_ref!r} is not a payment reference")
    return f"remit:RA-{match.group(1)}-{match.group(2)}"


def payment_ref_of(value: str) -> str | None:
    """`remit:RA-2024-0088` (or `RA-2024-0088`) -> `PAY-2024-0088`; None if it is not an id."""
    match = REMITTANCE_ID.match(value)
    return None if match is None else f"PAY-{match.group(1)}-{match.group(2)}"


def currency_of(advice: Advice) -> str:
    return CURRENCY_OF[advice.symbol]


def lines_of(advice: Advice) -> tuple[AdviceLine, ...]:
    return tuple(
        AdviceLine(invoice_ref=line.invoice_ref, amount_minor=line.amount_minor, note=line.note)
        for line in advice.lines
    )


def advice_record(advice: Advice, party: PartyRef, rate_ppm: int | None) -> CanonicalRecord:
    """One advice in the canonical shape.

    `rate_ppm` is the payment-date rate for a foreign advice (foreign units per GBP, parts per
    million) and None for a sterling one. Passing the wrong one of the two is a caller's bug.
    """
    currency = currency_of(advice)
    if (currency == BASE_CURRENCY) != (rate_ppm is None):
        raise ValueError(f"a {currency} advice and a rate of {rate_ppm!r} do not go together")
    return CanonicalRecord(
        record_id=remittance_id(advice.payment_ref),
        source="remittance",
        role="advice",
        amount=Money(
            minor=advice.total_minor,
            currency=currency,
            base_minor=(
                advice.total_minor
                if rate_ppm is None
                else to_base_minor(advice.total_minor, rate_ppm)
            ),
            fx_rate_ppm=rate_ppm,
            fx_rate_date=None if rate_ppm is None else advice.paid_on,
        ),
        party=party,
        record_date=advice.paid_on,
        reference=advice.payment_ref,
        free_text=advice.narrative,
        detail=RemittanceDetail(
            payment_ref=advice.payment_ref,
            lines=lines_of(advice),
            narrative=advice.narrative,
        ),
    )

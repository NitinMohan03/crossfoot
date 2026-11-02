"""Records that belong to no event but are still labelled (spec 01 §4.10).

Two kinds reach the bank CSVs. `batch_payout` lines come out of `emit/processor.py`, one per
batch. `refund` lines are built here: outbound `FPO` payments back to a counterparty, with
`Paid Out` populated, which is the only direction of money the rest of the dataset never shows.

Both sit outside the event quota -- only `open_invoice` and `unknown_receipt` count toward the
1% `no_match` rate -- but every one of them gets a group, so a matcher that claims them is
scored for it.
"""

from datetime import timedelta
from random import Random
from typing import Final

from crossfoot.generate.calendars import business_day_on_or_after
from crossfoot.generate.records import NoiseRecord, PaymentLeg
from crossfoot.generate.rng import substream
from crossfoot.generate.types.common import clean_surface_name, local_time
from crossfoot.generate.world import World

# ~50 in the full set (spec 01 §4.10). Slices scale with event count but keep a floor, so the
# dev slice still carries the class rather than dropping it to a handful.
FULL_EVENT_BASIS: Final = 10_000
REFUND_TARGET: Final = 50
REFUND_FLOOR: Final = 10
# A refund is a part-return of an earlier payment, not a whole invoice: keep them small.
REFUND_AMOUNT_MINOR: Final = (1_500, 250_000)


def refund_count(event_count: int) -> int:
    return max(REFUND_FLOOR, REFUND_TARGET * event_count // FULL_EVENT_BASIS)


def build_refunds(seed: int, world: World, event_count: int) -> list[NoiseRecord]:
    """Outbound `FPO` bank lines back to counterparties."""
    rng: Random = substream(seed, "noise:refunds")
    members = [cp for cp in world.counterparties if cp.currency == "GBP"]
    if not members:
        members = list(world.counterparties)
    span = (world.payment_end - world.invoice_start).days
    out: list[NoiseRecord] = []
    for _ in range(refund_count(event_count)):
        cp = rng.choice(members)
        day = business_day_on_or_after(
            world.invoice_start + timedelta(days=rng.randrange(span - 5))
        )
        out.append(
            NoiseRecord(
                kind="refund",
                leg=PaymentLeg(
                    kind="bank",
                    value_date=day,
                    amount_minor=rng.randrange(*REFUND_AMOUNT_MINOR),
                    currency="GBP",
                    payer_surface=clean_surface_name(rng, cp),
                    txn_type="FPO",
                    posted_time=local_time(rng),
                ),
                notes=f"refund to counterparty {cp.id}",
            )
        )
    return out

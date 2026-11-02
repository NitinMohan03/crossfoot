"""Shared construction helpers for the discrepancy-type builders.

Nothing here draws from the master stream: every function takes the event's own substream, so
a type's records depend only on (seed, type, ordinal) -- spec 01 §9.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from random import Random
from typing import Final

from crossfoot.config import Thresholds
from crossfoot.generate.calendars import (
    add_business_days,
    business_day_on_or_before,
    next_business_day,
)
from crossfoot.generate.fx import to_base_minor
from crossfoot.generate.records import Invoice, PaymentLeg
from crossfoot.generate.rng import weighted_pick
from crossfoot.generate.world import Counterparty, World
from crossfoot.money import ratio, round_half_up
from crossfoot.names import LEGAL_SUFFIXES, trivially_normalises

# (low, high, weight) in minor units. An integer stand-in for the log-normal in spec 01 §2:
# floats would make the draw depend on the platform's libm. Median lands near GBP 1,200.
AMOUNT_BUCKETS: Final = (
    (2_500, 10_000, 8),
    (10_000, 50_000, 20),
    (50_000, 150_000, 30),
    (150_000, 400_000, 22),
    (400_000, 1_500_000, 14),
    (1_500_000, 5_000_000, 5),
    (5_000_000, 25_000_000, 1),
)

ROUND_AMOUNT_PCT: Final = 10
ROUND_TO_MINOR: Final = 5_000  # GBP 50
DESCRIPTION_MAX: Final = 32
REFERENCE_STYLES: Final = (("exact", 40), ("compact", 20), ("digits", 20), ("po", 20))


@dataclass(frozen=True, slots=True)
class Pool:
    members: tuple[Counterparty, ...]
    weights: tuple[int, ...]

    def pick(self, rng: Random) -> Counterparty:
        if not self.members:
            raise ValueError("counterparty pool is empty")
        return weighted_pick(rng, self.members, self.weights)


@dataclass(frozen=True, slots=True)
class Pools:
    """Counterparty pools by payment capability. Card product implies a fee event (§4.2)."""

    direct: Pool
    pay_by_bank: Pool
    card: Pool
    non_card: Pool


def _pool(members: list[Counterparty]) -> Pool:
    return Pool(tuple(members), tuple(cp.weight for cp in members))


def build_pools(world: World, currency: str = "GBP") -> Pools:
    direct = [
        cp
        for cp in world.counterparties
        if cp.currency == currency and cp.payment_method == "direct"
    ]
    pay_by_bank = [
        cp
        for cp in world.counterparties
        if cp.currency == currency and cp.processor_product == "pay_by_bank"
    ]
    card = [cp for cp in world.counterparties if cp.currency == currency and cp.is_card]
    return Pools(
        direct=_pool(direct),
        pay_by_bank=_pool(pay_by_bank),
        card=_pool(card),
        non_card=_pool(direct + pay_by_bank),
    )


def sample_amount_minor(rng: Random) -> int:
    low, high, _ = weighted_pick(rng, AMOUNT_BUCKETS, [bucket[2] for bucket in AMOUNT_BUCKETS])
    return rng.randrange(low, high)


def round_to_fifty(amount_minor: int) -> int:
    rounded = (amount_minor + ROUND_TO_MINOR // 2) // ROUND_TO_MINOR * ROUND_TO_MINOR
    return max(ROUND_TO_MINOR, rounded)


def utc_moment(rng: Random, day: date, start_hour: int = 8, end_hour: int = 19) -> datetime:
    return datetime(
        day.year,
        day.month,
        day.day,
        rng.randrange(start_hour, end_hour),
        rng.randrange(60),
        rng.randrange(60),
        tzinfo=UTC,
    )


def local_time(rng: Random) -> tuple[int, int]:
    return rng.randrange(0, 24), rng.randrange(0, 60)


def latest_issue_date(world: World, cp: Counterparty) -> date:
    """Keep the whole payment window inside the dataset's date range (spec 01 §2)."""
    cutoff = world.payment_end - timedelta(days=cp.terms_days + 12)
    return min(world.invoice_end, cutoff)


def make_invoice(
    rng: Random,
    world: World,
    cp: Counterparty,
    *,
    allow_round_decoy: bool = False,
    issue_date: date | None = None,
    amount_minor: int | None = None,
) -> tuple[Invoice, list[str]]:
    """One invoice plus any decoy tags it earned."""
    if issue_date is None:
        span = (latest_issue_date(world, cp) - world.invoice_start).days
        issue_date = world.invoice_start + timedelta(days=rng.randrange(span + 1))
    amount = amount_minor if amount_minor is not None else sample_amount_minor(rng)
    decoys: list[str] = []
    if allow_round_decoy and amount_minor is None and rng.randrange(100) < ROUND_AMOUNT_PCT:
        amount = round_to_fifty(amount)
        decoys.append("round_amount")

    rate_ppm: int | None = None
    rate_date: date | None = None
    if cp.currency == "GBP":
        base = amount
    else:
        rate_date = issue_date
        rate_ppm = world.fx[rate_date][cp.currency]
        base = to_base_minor(amount, rate_ppm)

    invoice = Invoice(
        counterparty_id=cp.id,
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=cp.terms_days),
        currency=cp.currency,
        amount_minor=amount,
        base_amount_minor=base,
        created_at=utc_moment(rng, issue_date),
        fx_rate_ppm=rate_ppm,
        fx_rate_date=rate_date,
    )
    return invoice, decoys


def on_time_paid_on(rng: Random, invoice: Invoice) -> date:
    """A payment date that keeps the whole event inside clean's window.

    The ceiling is the last business day on or before the due date, so `paid_on + 1bd` -- the
    other date the payment side may carry -- still lands on or before `due + 1bd` and can
    never collide with `timing`, which starts at `due + 2bd` (spec 01 §4.1, §4.3).
    """
    ceiling = business_day_on_or_before(invoice.due_date)
    paid = add_business_days(ceiling, -rng.randrange(0, 9))
    floor = add_business_days(invoice.issue_date, 1)
    if paid < floor:
        paid = min(floor, ceiling)
    return paid


def value_date_for(rng: Random, paid_on: date) -> date:
    """paid_on, or the next business day (spec 01 §4.1)."""
    return paid_on if rng.randrange(2) else next_business_day(paid_on)


def truncate_words(text: str, limit: int) -> str:
    """Truncate on a word boundary so a partial token never survives."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] == " ":
        return cut
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip()


def strip_legal_suffix(name: str) -> str:
    tokens = name.split(" ")
    if tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    if len(tokens) > 1 and tokens[-1] in {"&", "AND"}:
        tokens.pop()
    return " ".join(tokens)


def clean_surface_name(rng: Random, cp: Counterparty) -> str:
    """A payer name that differs from the ERP legal name only trivially (spec 01 §4.1)."""
    name = " ".join(cp.legal_name.upper().split())
    style = rng.randrange(4)
    if style == 1:
        name = name.replace("&", "AND")
    elif style == 2:
        name = strip_legal_suffix(name)
    surface = truncate_words(name, 40)
    if not trivially_normalises(cp.legal_name, surface):
        raise AssertionError(f"clean payer name drifted too far: {cp.legal_name!r} -> {surface!r}")
    return surface


def reference_choice(rng: Random, present_pct: int) -> tuple[str, str | None]:
    """(style, po_number). Style "none" means the payer quoted nothing."""
    if rng.randrange(100) >= present_pct:
        return "none", None
    style = weighted_pick(
        rng, [name for name, _ in REFERENCE_STYLES], [weight for _, weight in REFERENCE_STYLES]
    )
    po_number = f"PO{rng.randrange(10_000, 100_000)}" if style == "po" else None
    return style, po_number


def card_fee_minor(cp: Counterparty, gross_minor: int) -> int:
    """round_half_up(gross x rate) + fixed (spec 01 §3.3)."""
    rate = Decimal(cp.fee_rate_bp) / Decimal(10_000)
    return round_half_up(Decimal(gross_minor) * rate) + cp.fee_fixed_minor


def assert_fee_window(gross_minor: int, net_minor: int) -> None:
    low, high = Thresholds.FEE_WINDOW
    observed = ratio(net_minor, gross_minor)
    if not low < observed < high:
        raise AssertionError(f"net/gross {observed} outside FEE_WINDOW for gross={gross_minor}")


def bank_leg(
    rng: Random,
    cp: Counterparty,
    *,
    value_date: date,
    amount_minor: int,
    reference_present_pct: int = 50,
) -> PaymentLeg:
    style, po_number = reference_choice(rng, reference_present_pct)
    return PaymentLeg(
        kind="bank",
        value_date=value_date,
        amount_minor=amount_minor,
        currency="GBP",
        payer_surface=clean_surface_name(rng, cp),
        reference_style=style,
        po_number=po_number,
        txn_type="FPI" if rng.randrange(100) < 70 else "BGC",
        posted_time=local_time(rng),
    )


def settlement_leg(
    rng: Random,
    cp: Counterparty,
    invoice: Invoice,
    *,
    settled_on: date,
    gross_minor: int,
    created_on: date | None = None,
) -> PaymentLeg:
    fee = card_fee_minor(cp, gross_minor) if cp.is_card else 0
    net = gross_minor - fee
    if fee:
        assert_fee_window(gross_minor, net)
    style, po_number = reference_choice(rng, 60)
    created_day = created_on or add_business_days(settled_on, -1)
    return PaymentLeg(
        kind="settlement",
        value_date=settled_on,
        amount_minor=gross_minor,
        currency=invoice.currency,
        payer_surface=clean_surface_name(rng, cp),
        fee_minor=fee,
        net_minor=net,
        reference_style=style,
        po_number=po_number,
        created_at=utc_moment(rng, created_day, 7, 22),
        product=cp.processor_product,
    )


@dataclass(frozen=True, slots=True)
class EventContext:
    """Everything a type builder may read. Not the master stream."""

    world: World
    pools: Pools
    index: int  # slot in the shuffled quota list
    ordinal: int  # index within this match type
    event_id: str

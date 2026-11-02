"""Shared construction helpers for the discrepancy-type builders.

Nothing here draws from the master stream: every function takes the event's own substream, so
a type's records depend only on (seed, type, ordinal) -- spec 01 §9.
"""

from collections.abc import Sequence
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
from crossfoot.generate.emit.refs import DESCRIPTION_MAX, description_for, truncate_words
from crossfoot.generate.fx import from_base_minor, to_base_minor
from crossfoot.generate.records import CreditNote, Invoice, PaymentLeg
from crossfoot.generate.rng import weighted_pick
from crossfoot.generate.world import Counterparty, ParentGroup, World
from crossfoot.money import ratio, round_half_up
from crossfoot.names import (
    LEGAL_SUFFIXES,
    best_similarity,
    surface_similarity,
    trivially_normalises,
)

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
    """Counterparty pools by payment capability and currency.

    Two world-level implications constrain every pool: a card product implies a `fee` event
    (§4.2) and a foreign currency implies an `fx` event (§4.7). So every class other than those
    two draws from `non_card`, which is base-currency and card-free by construction.
    """

    direct: Pool
    pay_by_bank: Pool
    card: Pool
    non_card: Pool
    # Foreign-currency, card-free: the only counterparties an `fx` event may use (§4.7).
    foreign: Pool
    # Base-currency, card-free members of a hidden parent group: `reasoning(a)` (§4.8).
    dba: Pool


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
    non_card = direct + pay_by_bank
    foreign = [cp for cp in world.counterparties if cp.currency != currency and not cp.is_card]
    grouped = {member_id for group in world.parents for member_id in group.member_ids}
    dba = [cp for cp in non_card if cp.id in grouped]
    return Pools(
        direct=_pool(direct),
        pay_by_bank=_pool(pay_by_bank),
        card=_pool(card),
        non_card=_pool(non_card),
        foreign=_pool(foreign),
        dba=_pool(dba),
    )


def parent_of(world: World, counterparty_id: int) -> ParentGroup | None:
    """The hidden parent group a counterparty belongs to, if any (spec 01 §2, §4.8)."""
    for group in world.parents:
        if counterparty_id in group.member_ids:
            return group
    return None


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


def latest_issue_date(world: World, cp: Counterparty, extra_days: int = 0) -> date:
    """Keep the whole payment window inside the dataset's date range (spec 01 §2).

    `extra_days` reserves room for a class that pays later than the usual few days after the
    due date -- `partial` spreads its instalments over up to 45 days (§4.5).
    """
    cutoff = world.payment_end - timedelta(days=cp.terms_days + 12 + extra_days)
    return min(world.invoice_end, cutoff)


def make_invoice(
    rng: Random,
    world: World,
    cp: Counterparty,
    *,
    allow_round_decoy: bool = False,
    issue_date: date | None = None,
    amount_minor: int | None = None,
    extra_days: int = 0,
) -> tuple[Invoice, list[str]]:
    """One invoice plus any decoy tags it earned."""
    if issue_date is None:
        span = (latest_issue_date(world, cp, extra_days) - world.invoice_start).days
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


ON_TIME_BACK_MAX: Final = 9


def on_time_paid_on(rng: Random, invoice: Invoice, back_bd: int | None = None) -> date:
    """A payment date that keeps the whole event inside clean's window.

    The ceiling is the last business day on or before the due date, so `paid_on + 1bd` -- the
    other date the payment side may carry -- still lands on or before `due + 1bd` and can
    never collide with `timing`, which starts at `due + 2bd` (spec 01 §4.1, §4.3).

    `back_bd` forces the number of business days to come back from the ceiling, which is how a
    decoy pair lands its two payments a known number of business days apart (§7).
    """
    ceiling = business_day_on_or_before(invoice.due_date)
    back = rng.randrange(0, ON_TIME_BACK_MAX) if back_bd is None else back_bd
    paid = add_business_days(ceiling, -back)
    floor = add_business_days(invoice.issue_date, 1)
    if paid < floor:
        paid = min(floor, ceiling)
    return paid


GROUP_SPREAD_CAP: Final = 30
# Days of the payment terms kept clear, so the payment window below is never empty.
GROUP_TERMS_RESERVE: Final = 5


def make_invoice_group(rng: Random, world: World, cp: Counterparty, count: int) -> list[Invoice]:
    """`count` invoices for one counterparty, clustered so one payment can cover them all.

    Drawing them independently across the year would put a payment that is on time for the
    earliest due date *before* a later invoice was even issued -- which is not a consolidated
    payment, it is nonsense. So the group spans at most `GROUP_SPREAD_CAP` days, and never more
    than the terms allow: an invoice issued within the spread must still be due after the whole
    group's payment lands (§4.6, §4.8c).
    """
    spread = max(0, min(GROUP_SPREAD_CAP, cp.terms_days - GROUP_TERMS_RESERVE))
    first, _ = make_invoice(rng, world, cp, extra_days=spread)
    latest = latest_issue_date(world, cp)
    out = [first]
    for _ in range(count - 1):
        issue = min(first.issue_date + timedelta(days=rng.randrange(spread + 1)), latest)
        out.append(make_invoice(rng, world, cp, issue_date=issue)[0])
    return out


def group_paid_on(rng: Random, invoices: Sequence[Invoice]) -> date:
    """One on-time payment date for a whole group of invoices.

    Bounded below by the last invoice's issue date -- you cannot pay an invoice that does not
    exist -- and above by the earliest due date, so the group stays inside clean's window.
    """
    ceiling = business_day_on_or_before(min(invoice.due_date for invoice in invoices))
    floor = add_business_days(max(invoice.issue_date for invoice in invoices), 1)
    if floor > ceiling:
        raise AssertionError(
            f"no group payment date: last issue+1bd {floor} is after earliest due {ceiling}"
        )
    paid = add_business_days(ceiling, -rng.randrange(0, ON_TIME_BACK_MAX))
    return max(paid, floor)


def split_exact(rng: Random, total_minor: int, parts: int, floor_minor: int = 1) -> list[int]:
    """Split `total_minor` into `parts` positive integers summing **exactly** to it.

    Used by `partial` (§4.5) and `consolidated` (§4.6), where the sum being exact is the whole
    property the matcher is tested on. Integer cut points, never a proportion of a float.
    """
    if parts < 1:
        raise ValueError("parts must be at least 1")
    if total_minor < parts * floor_minor:
        raise ValueError(f"cannot split {total_minor} into {parts} parts of >= {floor_minor}")
    if parts == 1:
        return [total_minor]
    spare = total_minor - parts * floor_minor
    cuts = sorted(rng.randrange(spare + 1) for _ in range(parts - 1))
    out: list[int] = []
    previous = 0
    for cut in cuts:
        out.append(floor_minor + cut - previous)
        previous = cut
    out.append(floor_minor + spare - previous)
    if sum(out) != total_minor:
        raise AssertionError(f"split {out} sums to {sum(out)}, not {total_minor}")
    return out


def value_date_for(rng: Random, paid_on: date) -> date:
    """paid_on, or the next business day (spec 01 §4.1)."""
    return paid_on if rng.randrange(2) else next_business_day(paid_on)


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


PAREN_SUFFIXES: Final = ("(UK)", "(HOLDINGS)", "(EUROPE)")
SWAP_SUFFIXES: Final = ("LTD", "LIMITED", "PLC", "& CO")
TYPO_SOURCE: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DRIFT_STYLES: Final = ("trading", "truncate", "paren", "typo", "swap_paren", "swap_typo")


def _swap_legal_suffix(rng: Random, name: str) -> str:
    """Replace the legal suffix with a different one (`LTD` -> `PLC`).

    Never used on its own: `names.normalise` strips every legal suffix, so a bare swap is a
    *trivial* difference and would make the case a `clean` one (spec 01 §4.1, §4.4).
    """
    stem = strip_legal_suffix(name)
    choices = [suffix for suffix in SWAP_SUFFIXES if not name.endswith(suffix)]
    return f"{stem} {rng.choice(choices)}" if choices else name


def _typo(rng: Random, name: str, count: int) -> str:
    """One or two single-character typos inside a non-suffix token."""
    stem = strip_legal_suffix(name)
    tail = name[len(stem) :]
    body = list(stem)
    positions = [index for index, char in enumerate(body) if char.isalpha()]
    if not positions:
        return name
    for _ in range(count):
        where = rng.choice(positions)
        body[where] = rng.choice([c for c in TYPO_SOURCE if c != body[where]])
    return "".join(body) + tail


def _apply_drift(rng: Random, cp: Counterparty, style: str) -> str | None:
    name = " ".join(cp.legal_name.upper().split())
    if style == "trading":
        trading = " ".join(cp.trading_name.upper().split())
        return None if trading == name else trading
    if style == "truncate":
        # Drop every token but the first, which removes the distinguishing trade word.
        head = name.split(" ")[0]
        return head if len(head) < 12 else None
    if style == "paren":
        return f"{name} {rng.choice(PAREN_SUFFIXES)}"
    if style == "typo":
        return _typo(rng, name, rng.randint(1, 2))
    if style == "swap_paren":
        return f"{_swap_legal_suffix(rng, name)} {rng.choice(PAREN_SUFFIXES)}"
    if style == "swap_typo":
        return _typo(rng, _swap_legal_suffix(rng, name), 1)
    raise ValueError(f"unknown drift style {style!r}")


def drifted_surface_name(rng: Random, cp: Counterparty, ordinal: int) -> tuple[str, str]:
    """A payer name drifted past trivial normalisation but still resolvable (spec 01 §4.4).

    Returns `(surface, style)`. Styles are tried in a rotation seeded by the event ordinal, so
    the mix is spread across the class rather than concentrated on whichever style happens to
    pass first. A candidate is accepted only when, as the bank will finally write it:

      * it is **not** `trivially_normalises` against the ERP legal name -- otherwise the case
        is a `clean` one, and the `name` class would not be testing anything; and
      * it scores >= `NAME_SIM_MIN` against the legal *or* the trading name, so a fuzzy
        matcher can still resolve it.
    """
    names = (cp.legal_name, cp.trading_name)
    offset = ordinal % len(DRIFT_STYLES)
    for step in range(len(DRIFT_STYLES)):
        style = DRIFT_STYLES[(offset + step) % len(DRIFT_STYLES)]
        candidate = _apply_drift(rng, cp, style)
        if candidate is None:
            continue
        surface = truncate_words(candidate, DESCRIPTION_MAX)
        if trivially_normalises(cp.legal_name, surface):
            continue
        if best_similarity(names, surface) < Thresholds.NAME_SIM_MIN:
            continue
        return surface, style
    raise AssertionError(f"no name drift satisfies both bounds for {cp.legal_name!r}")


def assert_drifted(cp: Counterparty, surface: str, description: str) -> None:
    """The two bars a `name` case must clear (spec 01 §4.4, §10.5).

    `surface` is the payer name as written; `description` is the whole field the bank wrote,
    which may also hold a reference the payer typed. The similarity floor is measured the way
    the validator measures it -- over token prefixes of `description`, since nothing in the
    field marks where the name ends.
    """
    if trivially_normalises(cp.legal_name, surface):
        raise AssertionError(f"name variant {surface!r} normalises to {cp.legal_name!r}")
    score = surface_similarity((cp.legal_name, cp.trading_name), description)
    if score < Thresholds.NAME_SIM_MIN:
        raise AssertionError(
            f"name variant {description!r} scores {score} against {cp.legal_name!r}, "
            f"below NAME_SIM_MIN {Thresholds.NAME_SIM_MIN}"
        )


def assert_unresolvable_name(cp: Counterparty, description: str) -> None:
    """`reasoning(a)`: the payer name must be out of fuzzy reach of both ERP names (§4.8).

    Measured over token prefixes too, so a matcher cannot recover the counterparty by lopping
    tokens off the description.
    """
    score = surface_similarity((cp.legal_name, cp.trading_name), description)
    if score >= Thresholds.NAME_SIM_MIN:
        raise AssertionError(
            f"dba payer {description!r} scores {score} against {cp.legal_name!r} / "
            f"{cp.trading_name!r}, at or above NAME_SIM_MIN {Thresholds.NAME_SIM_MIN}"
        )


def make_credit_note(
    rng: Random,
    world: World,
    cp: Counterparty,
    *,
    issued_on: date,
    base_amount_minor: int,
) -> CreditNote:
    """A credit note that offsets part of a consolidated payment (spec 01 §4.6).

    `issued_on` is clamped to the invoice window's last day: a `CN-2025-*` ref must never
    exist, because `reasoning(b)` cites one precisely to be unresolvable (§4.8).
    """
    day = min(issued_on, world.invoice_end)
    if cp.currency == "GBP":
        amount = base_amount_minor
    else:
        amount = from_base_minor(base_amount_minor, world.fx[day][cp.currency])
    return CreditNote(
        counterparty_id=cp.id,
        issued_on=day,
        currency=cp.currency,
        amount_minor=amount,
        base_amount_minor=base_amount_minor,
        created_at=utc_moment(rng, day),
    )


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
    payer_surface: str | None = None,
    preserve_name: bool = False,
    invoice_index: int = 0,
) -> PaymentLeg:
    """One bank credit. `payer_surface` overrides the clean name for the drifted classes."""
    style, po_number = reference_choice(rng, reference_present_pct)
    return PaymentLeg(
        kind="bank",
        value_date=value_date,
        amount_minor=amount_minor,
        currency="GBP",
        payer_surface=payer_surface or clean_surface_name(rng, cp),
        reference_style=style,
        po_number=po_number,
        txn_type="FPI" if rng.randrange(100) < 70 else "BGC",
        posted_time=local_time(rng),
        preserve_name=preserve_name,
        invoice_index=invoice_index,
    )


def settlement_leg(
    rng: Random,
    cp: Counterparty,
    invoice: Invoice,
    *,
    settled_on: date,
    gross_minor: int,
    created_on: date | None = None,
    payer_surface: str | None = None,
    preserve_name: bool = False,
    invoice_index: int = 0,
    fx_rate_ppm: int | None = None,
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
        payer_surface=payer_surface or clean_surface_name(rng, cp),
        fee_minor=fee,
        net_minor=net,
        reference_style=style,
        po_number=po_number,
        created_at=utc_moment(rng, created_day, 7, 22),
        product=cp.processor_product,
        preserve_name=preserve_name,
        invoice_index=invoice_index,
        fx_rate_ppm=fx_rate_ppm,
        fx_rate_date=settled_on if fx_rate_ppm is not None else None,
    )


def payment_leg(
    rng: Random,
    cp: Counterparty,
    invoice: Invoice,
    *,
    value_date: date,
    amount_minor: int,
    payer_surface: str | None = None,
    preserve_name: bool = False,
    invoice_index: int = 0,
    created_on: date | None = None,
    fx_rate_ppm: int | None = None,
) -> PaymentLeg:
    """A bank line for a `direct` counterparty, a settlement line for a processor one (§1).

    One call site for the "which source does the money land in" decision, which every class
    other than `fee` (always processor) and `no_match` (always bank) shares.
    """
    if cp.payment_method == "direct":
        return bank_leg(
            rng,
            cp,
            value_date=value_date,
            amount_minor=amount_minor,
            payer_surface=payer_surface,
            preserve_name=preserve_name,
            invoice_index=invoice_index,
        )
    return settlement_leg(
        rng,
        cp,
        invoice,
        settled_on=value_date,
        gross_minor=amount_minor,
        created_on=created_on,
        payer_surface=payer_surface,
        preserve_name=preserve_name,
        invoice_index=invoice_index,
        fx_rate_ppm=fx_rate_ppm,
    )


def assert_on_time(invoice: Invoice, value_date: date, label: str) -> None:
    """A clean-window payment never lands later than `due + 1bd` (spec 01 §4.1)."""
    limit = add_business_days(invoice.due_date, 1)
    if value_date > limit:
        raise AssertionError(f"{label} payment {value_date} later than due+1bd {limit}")


def surface_as_written(leg: PaymentLeg, invoice: Invoice | None) -> str:
    """What the matcher will read as the payer name on this leg.

    For a bank line that is the truncated CSV description; for a settlement line the payer name
    is its own field and survives whole. The name classes assert against this, not against the
    surface they constructed (spec 01 §4.4, §4.8).
    """
    if leg.kind != "bank":
        return leg.payer_surface
    return description_for(leg, invoice.ref if invoice and invoice.ref else None)


@dataclass(frozen=True, slots=True)
class DecoySpec:
    """A cross-event decoy constraint for one event slot (spec 01 §7).

    A decoy is a relation *between* events -- two invoices with the same amount, a counterparty
    with the same invoice every month -- so it cannot be drawn inside one event's substream.
    `crossfoot.generate.decoys` plans these from the master stream and hands one to the slot it
    constrains; the builder then constructs the event correctly from the start, so its own date
    and amount assertions still mean something.
    """

    tag: str  # amount_collision | near_amount | recurring
    counterparty_id: int
    issue_date: date
    amount_minor: int
    back_bd: int | None = None  # forced `on_time_paid_on` offset, for "paid N days apart"


@dataclass(frozen=True, slots=True)
class EventContext:
    """Everything a type builder may read. Not the master stream."""

    world: World
    pools: Pools
    index: int  # slot in the shuffled quota list
    ordinal: int  # index within this match type
    event_id: str
    # Cross-event decoy constraints for this slot, or None (spec 01 §7).
    decoy: DecoySpec | None = None
    # `reasoning` only: which sub-type this slot is, and its ordinal within that sub-type.
    reasoning_subtype: str | None = None
    sub_ordinal: int = 0

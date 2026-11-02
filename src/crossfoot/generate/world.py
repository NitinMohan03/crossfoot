"""The commercial world: counterparties, the hidden parent registry, and the FX table.

Everything here is a pure function of the seed. Names come from checked-in wordlists rather
than Faker, whose output is version-dependent and would break byte-determinism on upgrade
(spec 01 §2).
"""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from random import Random
from typing import Final

from crossfoot.generate.fx import build_fx_table
from crossfoot.generate.rng import substream, weighted_pick

INVOICE_START: Final = date(2024, 1, 1)
INVOICE_END: Final = date(2024, 12, 31)
PAYMENT_END: Final = date(2025, 1, 31)
FX_START: Final = date(2024, 1, 1)
FX_END: Final = date(2025, 1, 31)

FULL_COUNTERPARTIES: Final = 300
SLICE_COUNTERPARTIES: Final = 60
PARENT_GROUPS_PER_300: Final = 30
# A slice world is a fifth the size but carries >=100 of every type, so the pools the rarest
# classes draw from cannot be left to the currency and group dice (spec 01 §8.1).
MIN_PARENT_GROUPS: Final = 10
FOREIGN_FLOOR_DIVISOR: Final = 30
MIN_FOREIGN_NON_CARD: Final = 4

TERMS_CHOICES: Final = (14, 30, 45, 60)
TEMPLATES: Final = ("A", "B", "C", "D")
TEMPLATE_WEIGHTS: Final = (30, 30, 25, 15)
CURRENCY_WEIGHTS: Final = (("GBP", 90), ("USD", 5), ("EUR", 5))
COUNTRY_BY_CURRENCY: Final = {"GBP": "GB", "USD": "US", "EUR": "IE"}


@dataclass(frozen=True, slots=True)
class Counterparty:
    id: int
    legal_name: str
    trading_name: str
    country: str
    currency: str
    payment_method: str  # "processor" | "direct"
    terms_days: int
    processor_product: str | None  # "card" | "pay_by_bank" | None
    fee_rate_bp: int
    fee_fixed_minor: int
    remit_template: str
    created_at: datetime
    weight: int

    @property
    def is_card(self) -> bool:
        return self.processor_product == "card"


@dataclass(frozen=True, slots=True)
class ParentGroup:
    """A parent/DBA relationship the matcher is never shown (spec 01 §2, §4.8)."""

    parent_name: str
    member_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class World:
    seed: int
    kind: str
    counterparties: tuple[Counterparty, ...]
    parents: tuple[ParentGroup, ...]
    fx: dict[date, dict[str, int]]
    invoice_start: date = INVOICE_START
    invoice_end: date = INVOICE_END
    payment_end: date = PAYMENT_END

    def by_id(self, counterparty_id: int) -> Counterparty:
        return self.counterparties[counterparty_id - 1]


def _wordlist(name: str) -> tuple[str, ...]:
    raw = (files("crossfoot.generate.wordlists") / name).read_text(encoding="utf-8")
    return tuple(line.strip() for line in raw.splitlines() if line.strip())


def _name_pool(rng: Random, count: int) -> list[tuple[str, str]]:
    """`count` distinct (stem, trade) pairs, drawn without replacement."""
    stems = _wordlist("stems.txt")
    trades = _wordlist("trades.txt")
    combos = [(stem, trade) for stem in stems for trade in trades]
    if count > len(combos):
        raise ValueError(f"wordlists yield {len(combos)} names, need {count}")
    rng.shuffle(combos)
    return combos[:count]


def _activity_weight(rank: int) -> int:
    """Zipf-ish: the top 20 of 300 issue roughly 40% of invoices (spec 01 §2, approximate)."""
    return 10_000 // (rank + 5) + 5


def _ensure_foreign_pool(
    rng: Random, counterparties: list[Counterparty], floor: int
) -> list[Counterparty]:
    """Guarantee `floor` card-free foreign counterparties, converting the quietest if short.

    `fx` is the only class a foreign counterparty may belong to, and a card product already
    means `fee`, so the `fx` pool is exactly "foreign and card-free" (spec 01 §4.2, §4.7). At
    the full set's 300 counterparties the 10% foreign rate clears the floor on its own and this
    is a no-op; a 60-counterparty slice can fall short, and an empty pool would abort the run.
    Conversions take the lowest-weight counterparties so the activity distribution barely moves,
    and only card-free ones, so the `card` pool is untouched.
    """
    foreign = [cp for cp in counterparties if cp.currency != "GBP" and not cp.is_card]
    shortfall = floor - len(foreign)
    if shortfall <= 0:
        return counterparties
    candidates = sorted(
        (cp for cp in counterparties if cp.currency == "GBP" and not cp.is_card),
        key=lambda cp: (cp.weight, cp.id),
    )[:shortfall]
    converted = {cp.id for cp in candidates}
    out: list[Counterparty] = []
    for cp in counterparties:
        if cp.id not in converted:
            out.append(cp)
            continue
        currency = rng.choice(["USD", "EUR"])
        out.append(replace(cp, currency=currency, country=COUNTRY_BY_CURRENCY[currency]))
    return out


def build_world(seed: int, kind: str = "full", counterparty_count: int | None = None) -> World:
    count = counterparty_count or (FULL_COUNTERPARTIES if kind == "full" else SLICE_COUNTERPARTIES)
    name_rng = substream(seed, "world:names")
    profile_rng = substream(seed, "world:profiles")
    rank_rng = substream(seed, "world:ranks")
    parent_rng = substream(seed, "world:parents")

    suffixes = _wordlist("suffixes.txt")
    pool = _name_pool(name_rng, count + PARENT_GROUPS_PER_300)

    ranks = list(range(count))
    rank_rng.shuffle(ranks)

    counterparties: list[Counterparty] = []
    for index in range(count):
        stem, trade = pool[index]
        suffix = profile_rng.choice(suffixes)
        legal_name = f"{stem} {trade} {suffix}"
        trading_name = legal_name
        if profile_rng.randrange(100) < 25:
            trading_name = f"{stem} {trade}" if profile_rng.randrange(2) else stem
        currency = weighted_pick(
            profile_rng,
            [code for code, _ in CURRENCY_WEIGHTS],
            [weight for _, weight in CURRENCY_WEIGHTS],
        )
        payment_method = "processor" if profile_rng.randrange(100) < 60 else "direct"
        product: str | None = None
        if payment_method == "processor":
            product = "card" if profile_rng.randrange(100) < 40 else "pay_by_bank"
        created_at = datetime(2022, 1, 1, tzinfo=UTC) + timedelta(
            days=profile_rng.randrange(730), seconds=profile_rng.randrange(86400)
        )
        counterparties.append(
            Counterparty(
                id=index + 1,
                legal_name=legal_name,
                trading_name=trading_name,
                country=COUNTRY_BY_CURRENCY[currency],
                currency=currency,
                payment_method=payment_method,
                terms_days=profile_rng.choice(TERMS_CHOICES),
                processor_product=product,
                fee_rate_bp=profile_rng.randint(140, 350),
                fee_fixed_minor=profile_rng.randint(20, 30),
                remit_template=weighted_pick(profile_rng, list(TEMPLATES), list(TEMPLATE_WEIGHTS)),
                created_at=created_at,
                weight=_activity_weight(ranks[index]),
            )
        )

    counterparties = _ensure_foreign_pool(
        substream(seed, "world:foreign-floor"),
        counterparties,
        max(MIN_FOREIGN_NON_CARD, count // FOREIGN_FLOOR_DIVISOR),
    )

    # Scale with the world but never below `MIN_PARENT_GROUPS`: `reasoning(a)` draws from group
    # members only, and a slice still needs >=100 reasoning events. Capped by the name pool,
    # which holds exactly `PARENT_GROUPS_PER_300` spare pairs.
    group_count = min(
        PARENT_GROUPS_PER_300,
        max(MIN_PARENT_GROUPS, round(PARENT_GROUPS_PER_300 * count / FULL_COUNTERPARTIES)),
    )
    members = [cp.id for cp in counterparties]
    parent_rng.shuffle(members)
    parents: list[ParentGroup] = []
    cursor = 0
    for group_index in range(group_count):
        size = parent_rng.randint(1, 3)
        chunk = tuple(sorted(members[cursor : cursor + size]))
        cursor += size
        if not chunk:
            break
        stem, trade = pool[count + group_index]
        parents.append(ParentGroup(parent_name=f"{stem} {trade} GROUP PLC", member_ids=chunk))

    return World(
        seed=seed,
        kind=kind,
        counterparties=tuple(counterparties),
        parents=tuple(parents),
        fx=build_fx_table(substream(seed, "world:fx"), FX_START, FX_END),
    )

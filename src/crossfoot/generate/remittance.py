"""Which events carry a remittance advice, and what it says (spec 01 §3.4, §6, §6.4).

**§6.4, PDF presence must not leak the label.** Four classes always have an advice because the
advice is the only thing that resolves them: `partial`, `consolidated`, `reasoning(short_pay)`
and `reasoning(rounding)`. If nothing else had one, `has_pdf` would be a free classifier. So the
rest of the classes that could plausibly have one are topped up to a rate that brings the whole
dataset to ~40%.

That rate is derived from the **natural rate table**, not from the dataset in front of us:

    mandatory share  = partial 5 + consolidated 4 + 2/3 of reasoning 3   = 11 per 100
    toppable share   = clean 57 + fee 10 + timing 8 + name 7 + fx 3
                       + 1/3 of reasoning 3                              = 86 per 100
    target           = 40 per 100
    top-up rate      = (40 - 11) / 86                                    = 29/86 ~ 33.7%

Deriving it that way keeps each bucket independent: a bucket's count depends only on its own
size, so changing one class's quota cannot move which events in another class carry an advice
(spec 01 §9). `duplicate` and `no_match` never carry one -- an `unknown_receipt` with a
remittance advice would not be unknown.

**Session 1.3 resolution of §10.6.** The spec asks for each non-mandatory type's PDF rate to be
"within +/-3 pp of the global rate", but the global rate is 40% while those types sit at 33.7%
by §6.4's own arithmetic -- 6.3 pp away, so the assertion as literally written can never pass.
The reference is the pooled non-mandatory rate, which is the only reading under which `has_pdf`
is uninformative *between* those types. Integer rounding keeps every bucket inside 0.3 pp of it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from random import Random
from typing import Final

from crossfoot.generate.records import Event, RemitLine, Remittance
from crossfoot.generate.rng import substream
from crossfoot.generate.world import World
from crossfoot.money import format_minor

# A class whose advice is the only evidence that resolves it (spec 01 §4.5, §4.6, §4.8).
MANDATORY_TYPES: Final = ("partial", "consolidated")
MANDATORY_SUBTYPES: Final = ("short_pay", "rounding")
# Classes that get topped up to hide the signal.
TOPPABLE_TYPES: Final = ("clean", "fee", "timing", "name", "fx")
TOPPABLE_SUBTYPES: Final = ("dba",)
# Classes that never carry an advice.
NEVER_TYPES: Final = ("duplicate", "no_match")

TOP_UP_NUMERATOR: Final = 29
TOP_UP_DENOMINATOR: Final = 86
GLOBAL_TARGET_PCT: Final = 40

NARRATIVE_PCT: Final = 40
NARRATIVE_FILE: Final = "narratives.txt"
DEFAULT_TAG: Final = "general"
CURRENCY_SYMBOL: Final = {"GBP": "£", "USD": "$", "EUR": "€"}


def bucket_of(event: Event) -> str | None:
    """The top-up bucket an event belongs to, or None when it is not toppable."""
    if event.match_type in NEVER_TYPES or event.match_type in MANDATORY_TYPES:
        return None
    if event.match_type == "reasoning":
        subtype = event.reasoning_subtype
        if subtype in TOPPABLE_SUBTYPES:
            return f"reasoning:{subtype}"
        return None
    if event.match_type in TOPPABLE_TYPES:
        return str(event.match_type)
    return None


def top_up_count(bucket_size: int) -> int:
    """`bucket_size x 29/86`, rounded half up with integers only."""
    half = TOP_UP_DENOMINATOR // 2
    return (bucket_size * TOP_UP_NUMERATOR + half) // TOP_UP_DENOMINATOR


def assign(seed: int, events: Sequence[Event]) -> None:
    """Set `has_pdf` on the toppable events, exactly `top_up_count` per bucket."""
    buckets: dict[str, list[Event]] = {}
    for event in events:
        bucket = bucket_of(event)
        if bucket is not None:
            buckets.setdefault(bucket, []).append(event)

    for bucket, members in sorted(buckets.items()):
        chosen = list(members)
        # Each bucket draws from its own substream, so one class's selection never moves
        # another's (spec 01 §9).
        substream(seed, f"pdf:{bucket}").shuffle(chosen)
        for event in chosen[: top_up_count(len(members))]:
            event.has_pdf = True


@dataclass(frozen=True, slots=True)
class Corpus:
    """The narrative templates, grouped by tag (spec 01 §3.4)."""

    by_tag: dict[str, tuple[str, ...]]

    @classmethod
    def load(cls) -> "Corpus":
        raw = (files("crossfoot.generate.wordlists") / NARRATIVE_FILE).read_text(encoding="utf-8")
        grouped: dict[str, list[str]] = {}
        for line in raw.splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            tag, _, template = text.partition("|")
            if not template:
                raise ValueError(f"{NARRATIVE_FILE}: no '|' in {text!r}")
            grouped.setdefault(tag, []).append(template)
        return cls(by_tag={tag: tuple(items) for tag, items in sorted(grouped.items())})

    def pick(
        self,
        rng: Random,
        tag: str,
        *,
        event: Event,
        subsidiary: str,
        mandatory: bool,
    ) -> str | None:
        """A slot-filled narrative, or None when this advice carries no paragraph.

        `mandatory` is set for the classes where the narrative *is* the evidence -- a
        `short_pay` advice without its reason would make the case unsolvable rather than
        reasoning-solvable (spec 01 §4.8).
        """
        templates = self.by_tag.get(tag) or self.by_tag[DEFAULT_TAG]
        if not mandatory and rng.randrange(100) >= NARRATIVE_PCT:
            return None
        return _fill(rng.choice(templates), event, subsidiary)


def _cited_credit_note(event: Event) -> str:
    """The non-existent credit note a `short_pay` case cites, as recorded at build time."""
    for part in (event.notes or "").split(" "):
        if part.startswith("CN-"):
            return part
    raise AssertionError(f"{event.event_id}: a short_pay case must record the ref it cites")


def _fill(template: str, event: Event, subsidiary: str) -> str:
    lines = _lines_for(event)
    ref = lines[0].invoice_ref if lines else ""
    total = sum(line.amount_minor for line in lines)
    currency = event.invoices[0].currency if event.invoices else "GBP"
    replacements = {
        "{ref}": ref,
        "{count}": str(len(lines)),
        "{amount}": f"{CURRENCY_SYMBOL.get(currency, '')}{format_minor(total)}",
        "{subsidiary}": subsidiary,
    }
    if "{cn_ref}" in template:
        replacements["{cn_ref}"] = _cited_credit_note(event)
    out = template
    for slot, value in replacements.items():
        out = out.replace(slot, value)
    return out


def _narrative_tag(event: Event) -> str:
    if event.match_type == "reasoning":
        return str(event.reasoning_subtype)
    return str(event.match_type)


def _lines_for(event: Event) -> list[RemitLine]:
    """The line items the advice shows, which is not always what the ERP holds."""
    match_type = event.match_type
    subtype = event.reasoning_subtype

    if match_type == "partial":
        invoice = event.invoices[0]
        total = len(event.legs)
        return [
            RemitLine(invoice.ref, leg.amount_minor, f"instalment {position} of {total}")
            for position, leg in enumerate(event.legs, 1)
        ]

    if match_type == "consolidated":
        lines = [RemitLine(invoice.ref, invoice.base_amount_minor) for invoice in event.invoices]
        # A credit note is a negative contribution to the same payment (spec 01 §3.1).
        lines += [
            RemitLine(note.ref, -note.base_amount_minor, "credit note")
            for note in event.credit_notes
        ]
        return lines

    if subtype == "rounding":
        spread = _rounding_spread(event)
        return [
            RemitLine(
                invoice.ref,
                invoice.base_amount_minor + delta,
                "rounding adjustment" if delta else None,
            )
            for invoice, delta in zip(event.invoices, spread, strict=True)
        ]

    invoice = event.invoices[0]
    if subtype == "short_pay":
        return [RemitLine(invoice.ref, event.legs[0].amount_minor, "part payment, see note")]
    if match_type == "fx":
        return [RemitLine(invoice.ref, invoice.amount_minor)]
    return [RemitLine(invoice.ref, event.legs[0].amount_minor)]


def _rounding_spread(event: Event) -> list[int]:
    """The per-line pennies `reasoning(c)` recorded in `notes` at build time."""
    for part in (event.notes or "").split(" "):
        if part.startswith("spread="):
            return [int(value) for value in part.removeprefix("spread=").split(",")]
    raise AssertionError(f"{event.event_id}: a rounding case must record its per-line spread")


def build_all(seed: int, world: World, events: Sequence[Event], corpus: "Corpus") -> None:
    """Attach a `Remittance` to every event with `has_pdf`, numbered in payment order."""
    year = world.invoice_start.year
    wanted = sorted(
        (event for event in events if event.has_pdf),
        key=lambda event: (event.paid_on, event.index),
    )
    for number, event in enumerate(wanted, 1):
        if not event.legs:
            raise AssertionError(f"{event.event_id}: an advice needs a payment to advise on")
        payment_ref = f"PAY-{year}-{number:04d}"
        lines = _lines_for(event)
        cp = world.by_id(event.counterparty_id)
        rng = substream(seed, f"remit:{event.match_type}:{event.ordinal}")
        narrative = corpus.pick(
            rng,
            _narrative_tag(event),
            event=event,
            subsidiary=cp.legal_name,
            mandatory=_narrative_tag(event) in {"short_pay", "rounding", "dba"},
        )
        currency = event.invoices[0].currency if event.invoices else "GBP"
        event.remittance = Remittance(
            id=f"remit:RA-{year}-{number:04d}",
            payment_ref=payment_ref,
            template=cp.remit_template,
            payer=event.legs[0].payer_surface,
            paid_on=event.paid_on,
            currency=currency,
            total_minor=sum(line.amount_minor for line in lines),
            lines=lines,
            narrative=narrative,
        )

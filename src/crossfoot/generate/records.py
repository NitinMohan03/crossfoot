"""Intermediate records the type builders produce and the emitters consume.

Refs and bank line ids are deliberately absent at build time: invoice refs are sequential in
issue-date order across the whole dataset (spec 01 §2) and bank line ids depend on row order
within a month file (§3.2), so both are assigned in later passes.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from crossfoot.models.decision import MatchType


@dataclass(slots=True)
class Invoice:
    counterparty_id: int
    issue_date: date
    due_date: date
    currency: str
    amount_minor: int
    base_amount_minor: int
    created_at: datetime
    fx_rate_ppm: int | None = None
    fx_rate_date: date | None = None
    ref: str = ""  # assigned by assign_refs()

    @property
    def record_id(self) -> str:
        if not self.ref:
            raise ValueError("invoice ref not assigned yet")
        return f"erp:{self.ref}"


@dataclass(slots=True)
class CreditNote:
    counterparty_id: int
    issued_on: date
    currency: str
    amount_minor: int
    base_amount_minor: int
    created_at: datetime
    ref: str = ""

    @property
    def record_id(self) -> str:
        if not self.ref:
            raise ValueError("credit note ref not assigned yet")
        return f"erp:{self.ref}"


@dataclass(slots=True)
class PaymentLeg:
    """One record on the money side: a bank line or a processor settlement line."""

    kind: str  # "bank" | "settlement"
    value_date: date  # bank value date, or settlement settled_on
    amount_minor: int  # bank credit, or settlement gross
    currency: str
    payer_surface: str
    fee_minor: int = 0
    net_minor: int = 0
    reference_style: str = "none"  # none | exact | compact | digits | po
    po_number: str | None = None
    txn_type: str = "FPI"  # bank only: FPI | BGC | TFR | FPO
    created_at: datetime | None = None  # processor only
    product: str | None = None  # processor only
    posted_time: tuple[int, int] = (9, 0)  # bank only: local hour, minute
    record_id: str = ""  # assigned at emit time for bank lines
    batch_id: str = ""  # assigned by the processor emitter
    invoice_index: int = 0  # which invoice of the event this leg pays, for partials
    # Settlement-date rate for a foreign settlement (spec 01 §4.7). None for GBP.
    fx_rate_ppm: int | None = None
    fx_rate_date: date | None = None
    # True when the payer name carries the discrepancy (`name`, `reasoning(a)`): the bank
    # description must then never truncate it to make room for a reference fragment, or the
    # similarity the builder asserted would not be the one the matcher sees.
    preserve_name: bool = False


@dataclass(slots=True)
class RemitLine:
    """One line item on a remittance advice. `amount_minor` may differ from the ERP amount by
    the pennies `reasoning(c)` moves around (spec 01 §4.8)."""

    invoice_ref: str
    amount_minor: int
    note: str | None = None


@dataclass(slots=True)
class Remittance:
    """A remittance advice: one PDF plus its structured truth in `remit.jsonl` (spec 01 §3.4)."""

    id: str
    payment_ref: str
    template: str
    payer: str
    paid_on: date
    currency: str
    total_minor: int
    lines: list[RemitLine] = field(default_factory=list)
    narrative: str | None = None


@dataclass(slots=True)
class Event:
    index: int  # slot in the shuffled quota list, unique across the dataset
    ordinal: int  # index within this match type, which is what substream labels are keyed on
    event_id: str
    match_type: MatchType
    counterparty_id: int
    payment_method: str
    paid_on: date
    invoices: list[Invoice] = field(default_factory=list)
    credit_notes: list[CreditNote] = field(default_factory=list)
    legs: list[PaymentLeg] = field(default_factory=list)
    # Re-exported copies of a payment record (spec 01 §4.9). They land in the sources like any
    # other record but form their own `duplicate` group, so they are not the event's targets.
    duplicate_legs: list[PaymentLeg] = field(default_factory=list)
    has_pdf: bool = False
    decoys: list[str] = field(default_factory=list)
    notes: str | None = None
    reasoning_subtype: str | None = None
    no_match_kind: str | None = None  # open_invoice | unknown_receipt (spec 01 §4.10)
    remittance: Remittance | None = None  # filled by the remittance pass once ids exist

    @property
    def all_legs(self) -> list[PaymentLeg]:
        """Every payment record the event puts into a source, duplicates included."""
        return self.legs + self.duplicate_legs

    def invoice_for(self, leg: PaymentLeg) -> Invoice | None:
        """The invoice a leg pays, or None for an event that has no invoice at all."""
        if not self.invoices:
            return None
        return self.invoices[leg.invoice_index]


@dataclass(slots=True)
class NoiseRecord:
    """A record belonging to no event, labelled `no_match` with a kind (spec 01 §4.10)."""

    kind: str  # batch_payout | refund
    leg: PaymentLeg
    notes: str | None = None

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


@dataclass(slots=True)
class Event:
    index: int
    event_id: str
    match_type: MatchType
    counterparty_id: int
    payment_method: str
    paid_on: date
    invoices: list[Invoice] = field(default_factory=list)
    credit_notes: list[CreditNote] = field(default_factory=list)
    legs: list[PaymentLeg] = field(default_factory=list)
    has_pdf: bool = False
    decoys: list[str] = field(default_factory=list)
    notes: str | None = None
    reasoning_subtype: str | None = None


@dataclass(slots=True)
class NoiseRecord:
    """A record belonging to no event, labelled `no_match` with a kind (spec 01 §4.10)."""

    kind: str  # batch_payout | refund
    leg: PaymentLeg
    notes: str | None = None

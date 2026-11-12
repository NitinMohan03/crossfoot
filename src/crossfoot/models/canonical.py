"""The canonical schema every source normalises into (plan §2.4, §4.4).

Four heterogeneous sources -- the Postgres ERP, a UK bank CSV, processor settlement JSONL and
remittance PDFs -- land in one shape, so the matcher asks one set of questions instead of four.
The envelope is designed against all four at once: a new source adds a `detail` variant and
changes nothing else, which is what lets sessions 2.2-2.4 bring three more servers without
touching this file.

Three conventions, each enforced by a validator rather than written down and hoped for:

* **Money** is integer minor units (`Decimal` only where a ratio is unavoidable), never float.
  `base_minor` is the GBP view of `minor`; for GBP they are the same number.
* **Calendar dates stay dates.** `record_date` is the source's own calendar date and is never
  converted to UTC -- a conversion can shift it by a day (plan §2.7).
* **Timestamps are UTC**, with the source's original text kept beside them. The bank sidecar
  writes `Europe/London` local time, so the offset it carried is the evidence of the conversion
  and `event_time_source` is where that evidence lives.

Sign convention: positive is "owed to us" or "money in". An invoice is positive, a credit note
negative, a bank `Paid In` positive, a `Paid Out` refund negative. A payment record's `minor` is
the **gross** the payer paid; a processor line's fee and net sit in `ProcessorDetail`.

Record ids are spec 01 §5.3 ids, unchanged, so a canonical record joins the ground-truth
manifest with no translation step.
"""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crossfoot.config import Thresholds
from crossfoot.names import normalise

BASE_CURRENCY = "GBP"

SourceSystem = Literal["erp", "bank", "processor", "remittance"]
RecordRole = Literal["obligation", "payment", "advice"]
ResolutionMethod = Literal["exact", "normalised", "alias", "fuzzy", "unresolved"]

#: The id prefix each source's records carry (spec 01 §5.3).
ID_PREFIX: dict[SourceSystem, str] = {
    "erp": "erp:",
    "bank": "bank:",
    "processor": "proc:",
    "remittance": "remit:",
}

#: The role a source's records play. One source, one role -- an ERP row is always an obligation.
SOURCE_ROLE: dict[SourceSystem, RecordRole] = {
    "erp": "obligation",
    "bank": "payment",
    "processor": "payment",
    "remittance": "advice",
}

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class Frozen(BaseModel):
    """Immutable, and loud about fields it does not know."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Money(Frozen):
    """An amount in its own currency plus its GBP view. Integers throughout."""

    minor: int
    currency: Currency
    base_minor: int
    # Foreign units per GBP, parts per million -- the representation `ref.fx_rates` stores.
    fx_rate_ppm: int | None = None
    fx_rate_date: date | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.currency == BASE_CURRENCY:
            if self.base_minor != self.minor:
                raise ValueError(f"{BASE_CURRENCY} amount: base_minor must equal minor")
            if self.fx_rate_ppm is not None or self.fx_rate_date is not None:
                raise ValueError(f"{BASE_CURRENCY} amount carries no fx rate")
        if (self.fx_rate_ppm is None) != (self.fx_rate_date is None):
            raise ValueError("fx_rate_ppm and fx_rate_date go together or not at all")
        if self.fx_rate_ppm is not None and self.fx_rate_ppm <= 0:
            raise ValueError("fx_rate_ppm must be positive")
        if (self.minor > 0 and self.base_minor < 0) or (self.minor < 0 and self.base_minor > 0):
            raise ValueError("minor and base_minor disagree about sign")
        return self


class PartyRef(Frozen):
    """Who a record says it is about, and what entity resolution made of that.

    `surface` is the source's own text and is never edited; `normalised` is
    `crossfoot.names.normalise` applied to it, so every source normalises names the one way.

    `similarity` is the RapidFuzz score that identified the party, or 100 when the
    identification was structural rather than fuzzy -- an exact name, a trivial normalisation
    (including the bank's truncation), or a confirmed alias.
    """

    surface: str
    normalised: str
    counterparty_id: int | None = None
    method: ResolutionMethod = "unresolved"
    similarity: Annotated[int, Field(ge=0, le=100)] = 0

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.normalised != normalise(self.surface):
            raise ValueError("normalised must be names.normalise(surface)")
        if (self.method == "unresolved") != (self.counterparty_id is None):
            raise ValueError("a resolved party needs an id; an unresolved one must not have one")
        if self.method in ("exact", "normalised", "alias") and self.similarity != 100:
            raise ValueError(f"{self.method} resolution is exact, so similarity must be 100")
        if self.method == "fuzzy" and self.similarity < Thresholds.NAME_SIM_MIN:
            raise ValueError("a fuzzy match below NAME_SIM_MIN is not a match")
        return self


class ErpDetail(Frozen):
    """Filled by `mcp-erp`. The ERP is the obligation ledger and carries no payment status."""

    kind: Literal["erp"] = "erp"
    document: Literal["invoice", "credit_note"]
    counterparty_id: int
    terms_days: int | None = None


class BankDetail(Frozen):
    """Filled by `mcp-bank` (session 2.2)."""

    kind: Literal["bank"] = "bank"
    txn_type: Literal["FPI", "BGC", "TFR", "FPO"]
    balance_minor: int | None = None


class ProcessorDetail(Frozen):
    """Filled by `mcp-processor` (2.3). `fee + net == amount.minor`, checked on the record."""

    kind: Literal["processor"] = "processor"
    batch_id: str
    product: Literal["card", "pay_by_bank"]
    fee_minor: int
    net_minor: int

    @model_validator(mode="after")
    def _fee_belongs_to_a_card(self) -> Self:
        if self.product == "pay_by_bank" and self.fee_minor != 0:
            raise ValueError("pay_by_bank lines carry no fee (spec 01 §3.3)")
        return self


class AdviceLine(Frozen):
    """One line item on a remittance advice, as the advice states it.

    `amount_minor` is what the payer wrote, which `reasoning(c)` makes differ from the ERP
    amount by a few pence on purpose (spec 01 §4.8). Never reconcile it away at parse time.
    """

    invoice_ref: str
    amount_minor: int
    note: str | None = None


class RemittanceDetail(Frozen):
    """Filled by `mcp-remittance` (session 2.4).

    Deliberately no template id: `remit.jsonl` knows which layout produced a PDF, the PDF does
    not say, and a parser that could read it would not be tested on an unseen layout
    (spec 01 §6). The advice total is `amount.minor`, not a second field here.
    """

    kind: Literal["remittance"] = "remittance"
    payment_ref: str
    lines: tuple[AdviceLine, ...] = ()
    narrative: str | None = None


Detail = Annotated[
    ErpDetail | BankDetail | ProcessorDetail | RemittanceDetail,
    Field(discriminator="kind"),
]


class CanonicalRecord(Frozen):
    """One record from one source, in the one shape the matcher reads."""

    record_id: str
    source: SourceSystem
    role: RecordRole
    amount: Money
    party: PartyRef
    #: The source's own calendar date: ERP `issue_date`/`issued_on`, bank value date,
    #: settlement `settled_on`, advice `paid_on`. Never a converted timestamp.
    record_date: date
    #: Obligations only. Payments and advices have no due date.
    due_date: date | None = None
    #: UTC, tz-aware. The source's wall-clock moment, where it records one.
    event_time: datetime | None = None
    #: That moment exactly as the source wrote it, offset included.
    event_time_source: str | None = None
    #: A structured reference the source has a field for (an invoice ref, a payment ref).
    reference: str | None = None
    #: A reference the payer typed into a free-text field. May be a fragment, or noise.
    reference_surface: str | None = None
    #: Bank description, advice narrative -- text a human wrote.
    free_text: str | None = None
    detail: Detail
    #: The source line exactly as the source wrote it, where there is one line to keep (a JSONL
    #: record, a CSV row). None for a SQL row, which has no canonical text form.
    raw: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        prefix = ID_PREFIX[self.source]
        if not self.record_id.startswith(prefix):
            raise ValueError(f"a {self.source} record id starts with {prefix!r}")
        if self.detail.kind != self.source:
            raise ValueError(f"{self.source} record carries {self.detail.kind} detail")
        if self.role != SOURCE_ROLE[self.source]:
            raise ValueError(f"a {self.source} record is a {SOURCE_ROLE[self.source]}")
        if self.role != "obligation" and self.due_date is not None:
            raise ValueError("only an obligation has a due date")
        self._check_event_time()
        if isinstance(self.detail, ProcessorDetail):
            total = self.detail.fee_minor + self.detail.net_minor
            if total != self.amount.minor:
                raise ValueError(f"fee + net = {total} does not equal gross {self.amount.minor}")
        return self

    def _check_event_time(self) -> None:
        if self.event_time is None:
            if self.event_time_source is not None:
                raise ValueError("event_time_source without event_time keeps evidence of nothing")
            return
        offset = self.event_time.utcoffset()
        if offset is None:
            raise ValueError("event_time must be tz-aware UTC")
        if offset.total_seconds() != 0:
            raise ValueError("event_time must be UTC, not a local offset")

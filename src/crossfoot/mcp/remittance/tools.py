"""What the remittance server can be asked, as plain Python. Rule 1: every decision lives here.

Nothing in this module imports MCP. `server.py` registers these methods and turns their results
into payloads; the tests call them directly. Failures are `ToolError`s with a stable code, which
the server returns in the one envelope.

**An advice is addressed by the payment it advises on.** Its file is `<payment_ref>.pdf` and its
record id is that reference with another prefix (`canonical.py`), so a lookup by either opens
exactly one file and nothing is indexed. An argument is checked against the reference pattern
before any file name exists; what does not match is `invalid_argument` and never a path.

**An advice is read whole or not at all**, and what it says is checked against where it was
found: a PDF filed under one payment whose header names another is `malformed_source`.

**One advice that will not read does not hide the others.** A bank statement's line ids are
positions, so one bad row poisons its month; an advice is its own document with its own name.
The export therefore carries on past an unreadable advice -- and never silently: each one is
listed in `unparsed` with the code and the place it stopped, so whatever ingests the rest can
put that one in front of a person. Only `malformed_source` is treated that way. The counterparty
names being unreachable is not a fact about any one advice, and fails the call.

**Each advice is parsed once.** The files do not change within a run, and neither does a refusal:
a PDF that will not parse stays that way until someone replaces it.

**Order is the reference's:** year, then number, which is also payment order and the order of
the ids the keyset in `export_remittances` pages through.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from crossfoot.entities import resolve_description
from crossfoot.mcp.common.errors import (
    INVALID_ARGUMENT,
    MALFORMED_SOURCE,
    NOT_FOUND,
    UNAVAILABLE,
    ToolError,
)
from crossfoot.mcp.common.paging import page_limit
from crossfoot.mcp.common.parties import ErpParties, Parties
from crossfoot.mcp.remittance import canonical
from crossfoot.mcp.remittance.files import PAYMENT_REF, ReadOnlyAdvices, ref_key
from crossfoot.mcp.remittance.fx import Rates, parse_rates
from crossfoot.mcp.remittance.layout import read_pages
from crossfoot.mcp.remittance.parse import Advice, parse_advice
from crossfoot.mcp.remittance.settings import FX_FILE, RemittanceSettings
from crossfoot.models.canonical import BASE_CURRENCY, AdviceLine, CanonicalRecord, PartyRef


@dataclass(frozen=True, slots=True)
class AdviceLines:
    """The line items of one advice, with just enough of its header to read them by."""

    remittance_id: str
    payment_ref: str
    currency: str
    total_minor: int
    lines: tuple[AdviceLine, ...]


@dataclass(frozen=True, slots=True)
class Unparsed:
    """An advice the export could not read, and why."""

    remittance_id: str
    payment_ref: str
    error: ToolError


@dataclass(frozen=True, slots=True)
class RemittancePage:
    """One page of the export: what was read, what was not, and where to resume."""

    records: tuple[CanonicalRecord, ...]
    unparsed: tuple[Unparsed, ...]
    next_after_id: str | None
    has_more: bool

    @property
    def examined(self) -> int:
        return len(self.records) + len(self.unparsed)


def parse_payment_ref(value: str, field: str = "payment_ref") -> str:
    """A payment reference, or a coded refusal. The only door to a file name."""
    wanted = value.strip()
    if not wanted:
        raise ToolError(INVALID_ARGUMENT, f"{field} is empty")
    if PAYMENT_REF.match(wanted) is None:
        raise ToolError(
            INVALID_ARGUMENT, f"{field}: {value!r} is not a payment reference (PAY-YYYY-NNNN)"
        )
    return wanted


def parse_remittance_id(value: str, field: str = "remittance_id") -> str:
    """A remittance id (`remit:RA-2024-0088`, prefix optional) to its payment reference."""
    wanted = value.strip()
    if not wanted:
        raise ToolError(INVALID_ARGUMENT, f"{field} is empty")
    payment_ref = canonical.payment_ref_of(wanted)
    if payment_ref is None:
        raise ToolError(
            INVALID_ARGUMENT, f"{field}: {value!r} is not a remittance id (remit:RA-YYYY-NNNN)"
        )
    return payment_ref


def _again(error: ToolError) -> ToolError:
    """A remembered refusal, raised afresh so its traceback does not grow with every call."""
    return ToolError(error.code, error.message, **error.extra)


class RemittanceService:
    """The advice PDFs, read-only, with the names their payers are resolved against."""

    def __init__(
        self,
        settings: RemittanceSettings,
        parties: Parties | None = None,
        advices: ReadOnlyAdvices | None = None,
    ) -> None:
        self.settings = settings
        self.advices = advices or ReadOnlyAdvices(
            settings.remit_dir, settings.fx_path, settings.max_bytes
        )
        self.parties: Parties = parties or ErpParties(
            settings.dsn, settings.erp_schema, connect_timeout=settings.connect_timeout
        )
        self._parsed: dict[str, Advice | ToolError] = {}
        self._rates: Rates | None = None
        self._resolved: dict[str, PartyRef] = {}

    # -- plumbing ---------------------------------------------------------------------------

    def limit(self, limit: int | None) -> int:
        return page_limit(
            limit, default=self.settings.default_limit, maximum=self.settings.max_limit
        )

    def _advice(self, payment_ref: str) -> Advice | None:
        """One advice, parsed once. None when no advice was sent for that payment."""
        known = self._parsed.get(payment_ref)
        if isinstance(known, ToolError):
            raise _again(known)
        if known is not None:
            return known
        name = f"{payment_ref}.pdf"
        try:
            raw = self.advices.pdf_bytes(payment_ref)
            if raw is None:
                return None
            advice = parse_advice(name, read_pages(name, raw, max_pages=self.settings.max_pages))
            if advice.payment_ref != payment_ref:
                raise ToolError(
                    MALFORMED_SOURCE,
                    f"{name} is an advice for {advice.payment_ref}, not for the payment it is "
                    "filed under",
                    file=name,
                )
        except ToolError as exc:
            if exc.code == MALFORMED_SOURCE:
                self._parsed[payment_ref] = exc
            raise
        self._parsed[payment_ref] = advice
        return advice

    def _rate(self, advice: Advice) -> int | None:
        """The payment-date rate for a foreign advice; None for a sterling one."""
        currency = canonical.currency_of(advice)
        if currency == BASE_CURRENCY:
            return None
        if self._rates is None:
            text = self.advices.fx_text()
            if text is None:
                raise ToolError(
                    UNAVAILABLE,
                    f"{advice.payment_ref} is a {currency} advice and the dataset has no "
                    f"{FX_FILE} to value it in {BASE_CURRENCY} with",
                    file=FX_FILE,
                )
            self._rates = parse_rates(FX_FILE, text)
        rate = self._rates.get((advice.paid_on, currency))
        if rate is None:
            raise ToolError(
                MALFORMED_SOURCE,
                f"{FX_FILE} has no {currency} rate for {advice.paid_on.isoformat()}, the "
                f"payment date of {advice.payment_ref}",
                file=FX_FILE,
            )
        return rate

    def _resolve(self, payer: str) -> PartyRef:
        """Entity resolution, once per distinct name.

        `resolve_description`, not `resolve`: measured on this slice's 525 advices it gives two
        wrong counterparties where `resolve` gives four
        (`tests/test_entities/test_remittance_payers.py`).
        """
        if payer not in self._resolved:
            self._resolved[payer] = resolve_description(
                payer, self.parties.name_index(), self.parties.registry()
            )
        return self._resolved[payer]

    def _record(self, payment_ref: str) -> CanonicalRecord | None:
        advice = self._advice(payment_ref)
        if advice is None:
            return None
        return canonical.advice_record(advice, self._resolve(advice.payer), self._rate(advice))

    # -- the agent-facing tools -------------------------------------------------------------

    def get_remittance_for_payment(self, payment_ref: str) -> CanonicalRecord:
        """The remittance advice for one payment, by its payment reference (`PAY-2024-0088`)."""
        wanted = parse_payment_ref(payment_ref)
        record = self._record(wanted)
        if record is None:
            raise ToolError(
                NOT_FOUND,
                f"no remittance advice for payment {wanted!r}; most payments are sent without one",
            )
        return record

    def get_remittance_lines(self, remittance_id: str) -> AdviceLines:
        """The line items of one advice, by its id (`remit:RA-2024-0088`, prefix optional).

        Each line is what the payer wrote: the reference they cite and the amount they say it
        is for, which need not be the invoice's amount.
        """
        payment_ref = parse_remittance_id(remittance_id)
        wanted = canonical.remittance_id(payment_ref)
        advice = self._advice(payment_ref)
        if advice is None:
            raise ToolError(NOT_FOUND, f"no remittance advice with id {wanted!r}")
        return AdviceLines(
            remittance_id=wanted,
            payment_ref=payment_ref,
            currency=canonical.currency_of(advice),
            total_minor=advice.total_minor,
            lines=canonical.lines_of(advice),
        )

    def export_remittances(
        self, after_id: str | None = None, limit: int | None = None
    ) -> RemittancePage:
        """The bulk read ingestion uses: every advice in id order, keyset-paged on id.

        `limit` bounds the advices examined, so a page is a bounded amount of work whether or
        not they all read. `next_after_id` is the last one examined, read or not.
        """
        page = self.limit(limit)
        after: tuple[int, int] | None = None
        if after_id is not None and after_id.strip():
            after = ref_key(parse_remittance_id(after_id, "after_id"))
        waiting: Sequence[str] = [
            ref for ref in self.advices.payment_refs() if after is None or ref_key(ref) > after
        ]
        examined = waiting[:page]

        records: list[CanonicalRecord] = []
        unparsed: list[Unparsed] = []
        for payment_ref in examined:
            try:
                record = self._record(payment_ref)
            except ToolError as exc:
                if exc.code != MALFORMED_SOURCE:
                    raise
                unparsed.append(
                    Unparsed(
                        remittance_id=canonical.remittance_id(payment_ref),
                        payment_ref=payment_ref,
                        error=exc,
                    )
                )
                continue
            if record is not None:
                records.append(record)
        return RemittancePage(
            records=tuple(records),
            unparsed=tuple(unparsed),
            next_after_id=canonical.remittance_id(examined[-1]) if examined else None,
            has_more=len(waiting) > len(examined),
        )

    def close(self) -> None:
        self.parties.close()

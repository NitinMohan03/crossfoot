# crossfoot

Reconciles transactions across fragmented financial systems using deterministic rules first and an LLM only for the residual, with evals proving it works.

## Setup

Requires [uv](https://docs.astral.sh/uv/), [just](https://just.systems/), and Docker Desktop.

```bash
cp .env.example .env
just setup      # docker compose up -d --wait && uv sync (installs Python 3.12 if missing)
just check      # ruff + mypy + pytest
```

`just` with no arguments lists all recipes.

## Synthetic data and ground truth

Everything downstream is scored against a generated world and the manifest that records which
records belong to the same commercial event. The authority is
[`specs/01-data-generation.md`](specs/01-data-generation.md).

- **Jurisdiction and currency.** GBP base, England & Wales bank holidays, from the static list
  in `src/crossfoot/generate/reference/holidays_gb_eng.csv`. All business-day arithmetic in both
  the generator and the matcher reads that file and nothing else. Foreign currencies are USD and
  EUR. Money is integer minor units or `Decimal`, never float.
- **Four sources**, deliberately heterogeneous: a Postgres ERP dump (`erp.sql`), 13 monthly
  UK-style bank CSVs, processor settlements and batches as JSONL, and remittance-advice PDFs
  with a structured sidecar.
- **Scale.** 10,000 events at the natural discrepancy rates (clean 57%, fee 10%, timing 8%,
  name 7%, partial 5%, consolidated 4%, fx 3%, reasoning 3%, duplicate 2%, orphan 1%), plus
  labelled noise records.

```bash
just seed 42          # generate data/full
just seed-check 42    # prove a second run is byte-identical
just validate         # assert every ground-truth invariant, per-type table
just slice 123 scratch  # an independent 1,200-event slice in data/scratch
just golden "why"     # regenerate the committed dev slice (needs a reason)
```

`tests/fixtures/golden/dev/` is ground truth and is never hand-edited: a tool write is blocked
by a hook, the generator refuses to write there without `CROSSFOOT_REGEN_GOLDEN=1`, and
`just golden` refuses to leave the tree changed without a commit message. The test slice's seed
is held out until Phase 8 and `just slice` refuses to use it without `CROSSFOOT_ALLOW_TEST=1`.

### Realism costs, stated up front

- **Exactly one discrepancy type per event.** Compounding — a late payment to a renamed
  counterparty in a foreign currency — is not modelled.
- **The generator and the matcher share an author and share `Thresholds`.** Deterministic
  coverage on this data is therefore an **upper bound** on real-world coverage. The data is
  harder than the rules know in exactly three places: the `reasoning` class (asserted
  unsolvable against those shared thresholds), the held-out PDF template D (the parser is never
  developed against it and its parse rate is reported separately), and the hard-negative decoys.
  Nothing else here should be read as evidence of generalisation.
- One bank format, one holiday jurisdiction, three currencies, no OCR, no streaming.

## The canonical schema

Every source normalises into one envelope, `src/crossfoot/models/canonical.py`, so the matcher
asks one set of questions rather than four. It is designed against all four sources at once: a
new source adds a `detail` variant (`ErpDetail`, `BankDetail`, `ProcessorDetail`,
`RemittanceDetail`) and the envelope does not change.

Three conventions, each a validator rather than a comment:

- **Money** is integer minor units, with `base_minor` as the GBP view. For GBP they are the same
  number; a sign disagreement between the two is refused, as is an FX rate on a GBP amount.
- **Calendar dates stay dates.** `record_date` is the source's own date — ERP issue date, bank
  value date, settlement date, advice payment date — and is never converted to UTC, because the
  conversion can shift it by a day.
- **Timestamps are UTC**, tz-aware, with the source's original text kept in
  `event_time_source`. The bank sidecar writes `Europe/London` local time, so the offset it
  carried is the evidence that the conversion happened.

Record ids are the manifest's ids (`erp:INV-2024-0891`, `bank:2024-03-15:000004`,
`proc:stl_…`, `remit:RA-2024-0088`), unchanged, so a canonical record joins ground truth with no
translation step. Sign convention: positive is "owed to us" or "money in", so an invoice is
positive and a credit note negative.

## Entity resolution and the alias registry

`src/crossfoot/entities/` resolves a payer name to a counterparty in four layers — exact,
trivial normalisation (including the bank's 32-character truncation), a human-confirmed alias,
then RapidFuzz at `Thresholds.NAME_SIM_MIN`. **No layer answers by row order:** each collects
every counterparty it matches and answers only when there is exactly one, so two names that
collide after normalisation and two equal fuzzy scores both come back unresolved rather than
resolved by whichever row arrived first.

A bank description is a name with a typed reference possibly stuck on the end, and nothing marks
the boundary, so the resolver walks its token prefixes longest first. At each prefix it tries the
structural layers and then a fuzzy match **on that prefix alone**, scored
`min(token_set_ratio, ratio)`. That rule is a measurement, not a preference. On the dev slice's
881 bank lines that ground truth ties to an invoice, with an empty registry:

| Rule | Correct | Wrong | Unresolved |
|---|---|---|---|
| structural layers first, fuzzy last on the best `token_set_ratio` of any prefix | 847 | 22 | 12 |
| the same, with a fuzzy hit on a longer prefix allowed to outrank a structural one | 848 | 22 | 11 |
| per prefix, `min(token_set_ratio, ratio)` | 836 | **3** | 42 |

`token_set_ratio` scores a subset 100, and a one-word prefix is a subset of every name that
starts with that word, so the first rule handed 16 of the slice's 26 parent-company payers to an
unrelated counterparty sharing a first word — at a reported similarity of 100. The price of the
fix is 13 lone-first-word descriptions (`JARVIS`) that used to resolve and now abstain; they
resolved only because a 60-counterparty world holds one `JARVIS`. An abstention goes to review, a
wrong answer goes into the ledger. The three that remain wrong are descriptions that are,
character for character, a *different* counterparty's trading name; no string rule separates
those, and the amount and reference have to. `tests/test_entities/test_bank_descriptions.py`
keeps the numbers.

The registry (`er.counterparty_alias`) is the part Part 2 of the plan calls harder than the LLM
work, and it has two properties that the eval numbers depend on:

- **It learns only from human-confirmed escalations.** `provenance` is CHECK-constrained to
  `human_confirmed` and `escalation_id` is `NOT NULL`, so an alias derived from an LLM decision
  or an unreviewed match is not representable — not merely declined.
- **It is frozen during eval runs.** A registry is frozen unless something deliberately unfreezes
  it: `frozen` defaults to true, `CROSSFOOT_EVAL_RUN` forces it, and a frozen registry raises
  rather than silently skipping a write. A registry that learned from its own output would
  compound its own errors and make results depend on processing order; one that learned from the
  manifest would launder ground truth into the thing being measured.

## MCP servers

One server per source, stdio transport, **every tool read-only**. The agent's only write path is
the escalation queue.

`src/crossfoot/mcp/erp/` is the reference implementation; `src/crossfoot/mcp/__init__.py` states
the pattern the other three follow. Tools: `get_invoice`, `list_open_invoices`,
`get_credit_notes`, `lookup_alias` (the shared registry lookup) and `export_invoices` (the bulk
read ingestion uses). `vendor` arguments are free text and go through entity resolution, which
returns the counterparty it resolved to so a near miss is visible. `list_open_invoices` returns
every invoice in the window: the ERP is the obligation ledger and carries no payment status, so
whether one is settled is what reconciliation decides.

```bash
just db-load data/full   # load the ERP dump, then re-apply the read-only grants
just db-ref data/full    # FX rates and bank holidays into the `ref` schema
just db-er               # the alias registry table
just db-roles            # the crossfoot_ro role: CONNECT, USAGE, SELECT, nothing else
just mcp-erp             # serve the ERP over MCP on stdio
```

A failed call answers with `{"error": {"code", "message"}}` — codes `not_found`,
`invalid_argument`, `limit_exceeded`, `unresolved_vendor`, `unavailable`, `malformed_source`,
`read_only` — rather than failing the call, because an agent on a step budget has to branch on a
code, and an MCP error result carries only prose. `unavailable` is worth a retry (unless it says
`retryable: false`, as a refused credential does); `malformed_source` is not, and says which
file and row, which record, or which file, page and row of a PDF.

The page-size check on every `limit` argument is one function, `mcp/common/paging.py`, used by
all four servers: a `limit` below 1 is `invalid_argument`, one above the server's maximum is
`limit_exceeded` and carries `max_limit`.

Read-only is enforced three times over, each independently tested: the server connects as
`crossfoot_ro`, which holds no DML privilege; every transaction is opened read-only, so even a
DSN pointing at the owner refuses a write; and the advertised tool list is asserted equal to a
read-only allowlist. Note that `erp.sql` begins with `DROP SCHEMA IF EXISTS erp CASCADE`, which
takes the grants with it — `just db-load` re-applies them, and without that the no-write test
would pass for the wrong reason.

### The bank statement server

`src/crossfoot/mcp/bank/` serves the monthly bank CSVs. Tools: `get_bank_line`,
`find_bank_lines` (a signed amount window and/or a value-date window) and `export_bank_lines`
(the bulk read ingestion uses).

```bash
just mcp-bank data/full   # serve <DIR>/bank over MCP on stdio
```

The plan calls this source "fixed-width/CSV, encoding quirks". What the generator emits is plain
comma-separated CSV — no fixed-width columns, no quoting, pure ASCII, CRLF — so that is what is
parsed, and no parser was written for a format nothing produces. The quirks are handled where
they are real, on the way in: bytes that are not UTF-8 are a coded error rather than mojibake,
and a byte-order mark or LF line endings are read as what they are.

Everything that could move money or an id is strict. A bank line's id is its position —
`bank:YYYY-MM-DD:NNNNNN` is the value date and the row number in its month file — so a row that
will not parse is never skipped: skipping would hide a payment and renumber every line after it.
The running balance is checked row to row as the file's own checksum of that order. One bad
month fails the calls that touch it; the others keep answering.

This source is the project's test of the timezone position. `Transaction Date` is the value
date, a calendar date, and reaches `record_date` untouched. The posting moment comes from the
`posted_at.jsonl` sidecar in `Europe/London` local time, and is converted to UTC by the offset
it carries, with the original text kept. The dev slice straddles both clock changes:
`bank:2024-04-02:000003` was posted at `2024-04-02T00:02:00+01:00`, which is 23:02 UTC on the
**1st**, and its value date is still the 2nd. Thirty lines in the slice are like that. Had the
date been converted along with the timestamp, thirty payments would sit on the wrong day.

Payer names are resolved against the ERP's counterparties, read from Postgres through the same
read-only role — not from a copy on disk, which would be a second source of truth. If the
database is down the call answers `unavailable` instead of serving every line as coming from
nobody. Processor payout lines (`TFR`) are not resolved at all: the payer is the processor.

A directory has no role that cannot write, so read-only is drawn differently and tested four
ways: the advertised tools equal a read-only allowlist; the package holds exactly one `open`,
mode `"rb"`, and no call or import that changes a file (checked on the syntax tree); every tool,
on failing paths too, leaves a locked copy of the export byte for byte and timestamp for
timestamp as it was; and the connection the names come through refuses every write.

### The processor settlement server

`src/crossfoot/mcp/processor/` is a client of the mock processor API below — the first source
that is a network service. Tools: `get_settlement`, `list_settlements` (a settlement-date
window), `get_batch`, and the two bulk reads ingestion uses, `export_settlements` and
`export_batches`.

```bash
just proc-api data/full 8099                 # the API, in one shell
just mcp-processor http://127.0.0.1:8099     # the server over it, on stdio, in another
```

A settlement line becomes a payment record whose amount is the **gross**; the fee and the net
sit in `detail`, and `fee + net == gross` is checked on every record. A foreign line's sterling
view is its gross at the rate the processor recorded, held as integer parts per million — the
rate arrives as the text `"1.333209"` and never passes through a float. `settled_on` stays a
date; `created_at` becomes UTC with its original text beside it.

`get_batch` is the tool the bank source needs. The bank never sees a settlement line: it sees one
`TFR` line per batch, `PAYCO SETTLE BAT_2024-07-17_01`. Asked with that text exactly as the bank
writes it, the server returns the batch, the lines inside it, and `payout_base_minor` — and on
the dev slice all 268 payout lines open onto a batch whose payout is that bank line to the penny
and to the day. Before answering, the batch is checked against itself: its lines must number its
`count` and their nets must add up to its total. A batch is not a canonical record; it is what
ties one bank line to many settlement lines.

What the source being HTTP changes:

- **Read-only is the verb.** There is no role to connect as and no file mode to pick, so the
  package issues one request, in one module, and its method is the literal `GET` with no body.
  That is checked on the syntax tree, then checked again on what arrives at the API and on what
  goes over a real socket. Redirects are not followed and no proxy is used: the bearer token goes
  to the configured host and nowhere else.
- **The token** is read from `CROSSFOOT_PROCESSOR_TOKEN` — the same variable the API reads — and
  has no command-line flag. It is sent in the `Authorization` header only, is left out of the
  settings' `repr`, and never appears in an error: messages are built from the API's stable
  code, not from its prose.
- **A 429 is waited out, a bounded number of times.** `Retry-After` is honoured exactly, at most
  twice per request; a longer wait than five seconds is not slept at all but handed back as
  `unavailable` with `retry_after`, so the caller decides. Timeouts and 5xx are reported once.
- **Every failure lands on an existing code.** A refused credential is `unavailable` with
  `retryable: false` — no argument caused it and none fixes it. A body that is not JSON, a field
  nobody documented, an amount written `1.0`, a line whose `fee + net` is not its gross: all
  `malformed_source`, naming the record. A forged cursor is the caller's `invalid_argument`.

Which resolver reads `payer_name` was measured rather than assumed. It is a bare name, which is
what `resolve()` is for — and on the dev slice's 552 lines `resolve()` gets 535 right, 7 wrong
and leaves 10 unresolved, while `resolve_description()` gets 529 right, **1** wrong and leaves
22. Six of the seven are a payer who shortened their name to its first word, matched at a
"perfect" 100 to another company starting with that word. The server uses the second: an
unresolved payment goes to review, a wrong one goes into the ledger.

### The remittance advice server

`src/crossfoot/mcp/remittance/` serves the remittance advice PDFs — the fourth and last source.
Tools: `get_remittance_for_payment` (by payment reference, `PAY-2024-0088`),
`get_remittance_lines` (by id, `remit:RA-2024-0088`) and `export_remittances` (the bulk read
ingestion uses).

```bash
just mcp-remittance data/full   # serve <DIR>/remit over MCP on stdio
```

**Parsed structurally, not chunked.** A line item means nothing without the header naming the
payer, so cutting the page into passages of text is simply the wrong tool here. `layout.py`
turns a PDF into *text runs* — one per string the sender drew, with its position — and
`parse.py` reads the page as the form it is: a title, a header naming the payer and the payment,
a table whose columns are found by their headings rather than their order, a total, and
sometimes a paragraph. The unit read and the unit returned is the whole advice. Every run on the
page has to land in one of those regions; text the reader has no place for is a refusal, not
something to read around.

It is strict wherever a mistake could move money or an id. An amount is digits, a point and two
digits, built into integer minor units from its digits — `1,250.00` is a different sender, not a
rounding choice. The lines must add up to the stated total, a second statement of the total must
agree with the first, a running balance must follow from the line above, the currency symbol
must be the same wherever it appears, and the reference in the header must be the payment the
file is named for. Each of those failing is `malformed_source`, with the file, page and row.

**What the payer wrote is not reconciled.** A record's `amount` is the total the advice states
and `detail.lines` are its lines as printed. 29 of the dev slice's developed advices differ from
the ledger by one to five pence on purpose, and 29 more are short payments whose reason is in
the paragraph; "correcting" either at parse time would delete the one fact the matcher needs
the advice for. The paragraph is kept whole in `free_text` and is not mined: an advice from a
parent company names the subsidiary only there, and its `party` stays unresolved.

Three things the advice does not say, and what the server does about each:

- **Its own id.** An advice prints the payment reference and nothing else, so the record id
  `remit:RA-2024-0088` is derived from `PAY-2024-0088` — same year, same number — and that rule
  is tested against every advice ground truth lists.
- **Its currency, except as a symbol.** `£`, `$`, `€` map to the three currencies this business
  invoices in.
- **A sterling amount, if it is foreign.** `base_minor` is then the total at the rate for the
  advice's own payment date, with that rate and date on the record so the inference is visible.
  The rate comes from the dataset's own `fx_rates.csv`, not from `ref.fx_rates`: that table is a
  projection of one dataset's file and holds whichever was loaded last, so an advice from one
  dataset could be valued at another's rates with nothing to show for it.

One thing it cannot do, found while building it: **the payment reference appears on no bank or
processor record**, only on the advice. An agent holding a bank line cannot call
`get_remittance_for_payment` cold, and the date and total it could search by are inside the
PDFs. Tying a payment to its advice is therefore ingestion's job — load every advice through
`export_remittances`, then block on date, amount and party — after which the reference is known.

Read-only is drawn as for the bank, and tested the same four ways: an allowlist of advertised
tools; one `open` in the package, mode `"rb"`, with the PDF library handed bytes in memory and
never a path (checked on the syntax tree); a locked copy of a dataset left byte for byte and
timestamp for timestamp as it was, on failing paths too; and a names connection that refuses
every write.

Which resolver reads the payer name was measured, for the third time. On the slice's 525
advices `resolve()` gets 508 right, 4 wrong and leaves 13 unresolved; `resolve_description()`
gets 499 right, **2** wrong and leaves 24. The server uses the second. The two it puts right are
a lone first word matched at 100 and a typo matched at 89; the two still wrong are, character
for character, another counterparty's trading name.

#### The held-out layout

The generator draws four remittance layouts, and one — D, 81 of the dev slice's 525 advices — is
reserved: the parser is developed against the other three and then measured on a page design it
was not written for. `remit.jsonl`, which records what every advice says and which layout drew
it, is the answer key; it sits in the same directory as the PDFs, and the server neither lists
nor opens it (also checked on the syntax tree, and by serving identical answers with that file
replaced by garbage).

| Layouts | Advices | Exact | Misread | Refused |
|---|---|---|---|---|
| Developed (A, B, C) | 444 | **444** | 0 | 0 |
| Held out (D) | 81 | **0** | **0** | **81** |

"Exact" is every field equal to `remit.jsonl`: payer, reference, date, total, each line's
reference, amount and note, and the paragraph. **The parse rate on the held-out layout is 0 of
81.** All 81 are refused at the same place — the page has no row of column headings the reader
knows — and none is misread, which matters more than the rate: a refusal goes to a person, a
misread goes into the ledger looking like an answer. The layer underneath carried over intact
(all 81 PDFs become text runs opening with the right title); what did not generalise is the
vocabulary of the form. Across the slice, 444 of 525 advices (84.6%) are read, and
`export_remittances` returns the other 81 by name in `unparsed`, each with its coded error, so
ingestion can queue them rather than lose them.

How the number was produced, since a held-out number is only as good as the holding out. Every
development test runs against a temporary dataset that contains only the developed PDFs, so no
other test *can* open a held-out one. The parser and server were committed first; the
measurement was then run once and pinned in `tests/test_mcp/remittance/test_held_out.py`
without the parser being touched. That test fingerprints the two modules that do the reading
and fails if either changes, because the number would then describe code that no longer exists.

One limit on that, stated plainly: the session that wrote the parser had been told to read the
generator's `remit_pdf.py`, which draws all four layouts, so its author knew how D differs
before measuring. No D file was opened and no rule was added that A–C do not exercise — a
result of nothing parsed is at least consistent with that — but the blind was procedural, not
perfect. Teaching the parser layout D is an ordinary follow-up; when that happens D stops being
held out, and this table keeps its 0 / 0 / 81 beside whatever replaces it.

## Mock processor API

The processor source is a REST API, so one exists to wrap: `src/crossfoot/mock_processor/`
serves the generated settlement data behind the three behaviours a real client must handle.

```bash
just proc-api data/full 8099    # http://127.0.0.1:8099
```

| Endpoint | Returns |
|---|---|
| `GET /healthz` | unauthenticated, unmetered liveness, plus the settlement count |
| `GET /v1/settlements` | a page of settlement lines; `limit`, `cursor`, `settled_from`, `settled_to` |
| `GET /v1/settlements/{id}` | one settlement line |
| `GET /v1/batches` | a page of batches; `limit`, `cursor`, `closed_from`, `closed_to` |
| `GET /v1/batches/{id}` | one batch |

Dev token: `dev-processor-token`, sent as `Authorization: Bearer dev-processor-token` and
overridable with `CROSSFOOT_PROCESSOR_TOKEN`. The three behaviours a client must handle:

1. **Cursor pagination.** A list response is `{"data": [...], "next_cursor": ..., "has_more":
   ...}`. The cursor is opaque, signed, and scoped to its collection — a settlements cursor is
   rejected on batches with `bad_cursor`. Walk until `has_more` is false.
2. **Bearer auth.** 401 with `WWW-Authenticate: Bearer` and a distinct code for each cause:
   `missing_token`, `invalid_scheme`, `invalid_token`.
3. **Rate limiting.** 429 with `Retry-After` in whole seconds and a `retry_after` field; the
   advertised wait is exact, and a client that backs off by it succeeds.

Records are served as the JSON lines the generator wrote, byte for byte, never re-serialised —
so no amount is ever parsed into a float on the way through.

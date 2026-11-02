# Spec 01 — Synthetic data generation and ground truth

Status: agreed in Session 1.1 interview (2026-09-14). Implemented in Sessions 1.2–1.3.
Supersedes the provisional notes in `.claude/skills/fixture-conventions/SKILL.md`; that skill
should be updated to point here once 1.2 lands.

Items marked **[decided]** were not raised in the interview and were settled by the interviewer
using the plan's constraints. Override any of them before Session 1.2 starts, not after.

---

## 1. Purpose and vocabulary

The generator emits one commercial world across four sources plus a manifest recording the true
match set. Everything downstream (rules, blocking, LLM adjudication, eval) is measured against
this manifest. A bug here invalidates every number in the project.

- **Event** — one commercial fact: a counterparty pays for one or more invoices. Rates are per event.
- **Record** — one row in one source. A record has exactly one id and appears in exactly one manifest group.
- **Payment record** — the record on the money side that an ERP invoice is matched to: a **bank line** for direct transfers, a **processor settlement line** for processor-paid events (§3.2).
- **Group** — the scoring unit: `{source_ids} → {target_ids}` plus `match_type`. See §5.
- **Noise record** — a record that belongs to no event (batch payouts, refunds). Labelled `no_match` with a `kind`.

Non-goals for Phase 1: real bank formats beyond one UK-style CSV, OCR'd PDFs, more than one holiday jurisdiction, more than three currencies, streaming.

---

## 2. World parameters

| Parameter | Value |
|---|---|
| Base currency / jurisdiction | GBP, England & Wales bank holidays. Stated in README. |
| Foreign currencies | USD, EUR |
| Counterparties | ~300 in the full set; activity Zipf-like (top 20 issue ~40% of invoices) |
| Invoice date range | 2024-01-01 .. 2024-12-31; payments may land through 2025-01-31 |
| Bank CSVs | one per month, 2024-01 .. 2025-01 (13 files) |
| Invoice amounts | log-normal, median ~£1,200, clipped £25 .. £250,000, 2 dp; ~10% rounded to £50 multiples (decoy, §7) |
| Invoice refs | `INV-2024-NNNN`, sequential in issue-date order; credit notes `CN-2024-NNNN` |
| Payment terms | per counterparty: 14, 30, 45 or 60 days; due date = issue + terms |
| Payment method | per counterparty: ~60% `processor` (card or pay-by-bank), ~40% `direct` (BACS / Faster Payments) |
| Remittance PDFs | ~40% of payments overall; 100% for `partial`, `consolidated`, `reasoning(b)`, `reasoning(c)`; balanced so PDF presence does not leak the label (§6.4) |
| Scale | 10,000 events in the full set; slices per §8 |

Counterparty names come from word lists checked into the repo (`src/crossfoot/generate/wordlists/`), not Faker — Faker's output is version-dependent and would break byte-determinism across upgrades. **[decided]**

Each counterparty has: `legal_name`, `trading_name` (may equal legal), `country`, `currency`, `payment_method`, `terms_days`, `processor_product` (`card` | `pay_by_bank`, processor-paid only). A separate **parent/DBA registry** (~30 parent groups, each with 1–3 subsidiaries or trading styles) is generated alongside but written **only** to the manifest directory, never into the ERP dump — the matcher must not see it (§4.8).

---

## 3. The four sources

### 3.1 ERP — Postgres via `erp.sql`

Loaded by `just seed` into the `erp` schema. Tables:

- `counterparties(id, legal_name, trading_name, country, currency, payment_method, terms_days, created_at timestamptz)`
- `invoices(ref, counterparty_id, issue_date date, due_date date, currency, amount_minor bigint, base_amount_minor bigint, fx_rate numeric(12,6), fx_rate_date date, created_at timestamptz)`
- `credit_notes(ref, counterparty_id, issued_on date, currency, amount_minor bigint, base_amount_minor bigint, created_at timestamptz)`

`amount_minor` is in the invoice currency; `base_amount_minor` is GBP at the invoice-date rate (equal when currency is GBP). Credit notes are positive amounts with their own ref; the manifest treats them as negative contributions in a consolidated group.

### 3.2 Bank — CSV

UK current-account export. One file per month, `bank/2024-03.csv`, CRLF, UTF-8, header:

```
Transaction Date,Transaction Type,Description,Paid Out,Paid In,Balance
```

- `Transaction Date` is `DD/MM/YYYY`; it is the **value date**, a calendar date.
- `Transaction Type` ∈ `FPI` (Faster Payment in), `BGC` (BACS credit), `TFR` (processor batch payout), `FPO` (Faster Payment out — refunds).
- `Description` ≤ 32 characters, upper-case, as a bank would truncate it: payer name (drifted per §4.4) and, for ~50% of direct transfers, a reference fragment the payer typed (`INV-2024-0891`, `INV0891`, `0891`, or their own PO number). Never an exact structured invoice ref column.
- `Paid In` / `Paid Out` are strings with 2 dp, one of them empty. `Balance` is a deterministic running balance.
- Bank line id: `bank:YYYY-MM-DD:NNNNNN`, sequential within the file by row order. Row order = value date, then a deterministic shuffle (§9).
- A sidecar `bank/posted_at.jsonl` **[decided]** carries `{id, posted_at}` with `posted_at` in `Europe/London` local time with offset (`2024-03-31T00:30:00+00:00` vs `+01:00` after the DST switch) so UTC normalisation is exercised at ingest. The CSV itself has no timestamp column, matching real exports.

**Processor-paid events do not produce a per-invoice bank line.** The bank sees one `TFR` line per processor batch payout (§3.3). Those lines are noise records: `no_match`, `kind=batch_payout`, linked to their batch id via `notes`.

### 3.3 Processor — `processor/settlements.jsonl` + `processor/batches.jsonl`

Consumed by the Phase 2.0 mock REST API. Integer minor units throughout.

Settlement line:
```json
{"id":"proc:stl_7f3a…","batch_id":"bat_2024-03-14_02","product":"card","payer_name":"ACME CORP LTD","payer_reference":"INV-2024-0891","currency":"GBP","gross_minor":441700,"fee_minor":6444,"net_minor":435256,"created_at":"2024-03-13T16:42:07Z","settled_on":"2024-03-14"}
```
- `product=card` lines carry a fee: `fee = round_half_up(gross × rate) + fixed`, rate per counterparty in 1.4–3.5%, fixed 20–30p. `product=pay_by_bank` lines have `fee_minor=0`.
- `payer_reference` present on ~60% of lines; when present it is either the exact invoice ref or the payer's PO number.
- `created_at` is UTC ISO-8601 with `Z`; `settled_on` is a calendar date.
- Batch: `{"id":"bat_2024-03-14_02","closed_on":"2024-03-14","paid_out_on":"2024-03-15","count":48,"total_net_minor":…}`; ~50 lines per batch, one or two batches per business day; payout lands in the bank on the next business day as a `TFR` line whose amount equals `total_net_minor`.

### 3.4 Remittance advices — `remit/<payment_ref>.pdf` + `remit/remit.jsonl`

Generated with reportlab in **invariant mode** (fixed creation date, no `/ID`, built-in fonts only) so bytes are stable. Four layout templates (§6). Each PDF has:

- header: payer name (drifted per §4.4), payment reference, payment date, total;
- line items: invoice ref (template-dependent format), amount, optional per-line note;
- optional narrative paragraph drawn from a corpus of ~50 templates (short-pay reasons, disputes, "see CN", "paid in two instalments", rounding adjustment, …) with slot-filled refs and amounts.

`remit.jsonl` is the structured truth per PDF (`{id, template, payer, payment_ref, paid_on, total_minor, lines:[{invoice_ref, amount_minor, note}], narrative}`) including the held-out template, so parse rate can be measured on it. Remittance ids: `remit:RA-2024-NNNN`.

### 3.5 Companion tables

- `fx_rates.csv` — daily, 2024-01-01 .. 2025-01-31, GBP base, columns `date,USD,EUR`, 6 dp. Random walk ±0.5%/day from 1.27 / 1.16; weekends and holidays carry the previous business day's rate. Loaded into Postgres in Phase 2.0.
- `holidays_gb_eng.csv` — England & Wales bank holidays 2024–2025, checked into the repo as a static list (`date,name`), no library dependency. Business-day arithmetic in the generator and matcher use this file and nothing else.

---

## 4. Discrepancy types

Exactly one type per event. Compounding is not modelled; the README states this as a realism cost.

Quotas are exact, not sampled: the generator assigns types to event slots by quota then shuffles deterministically, so the validator asserts exact counts (§10).

Thresholds shared with the matcher live in one place, `src/crossfoot/config.py::Thresholds` **[decided]**:

```
NAME_SIM_MIN     = 80          # RapidFuzz token_set_ratio, 0–100
FEE_WINDOW       = (Decimal("0.90"), Decimal("1.02"))   # net / gross
DATE_WINDOW_BD   = (-1, +5)    # business days around due date
EXACT_SUM_TOL    = 0           # minor units
```

The generator imports these to *construct* and *assert* cases; the matcher imports them to *decide*. Changing one changes both, which is the point.

### 4.1 `clean` — 57%

- Names: ERP `legal_name` vs bank/processor/remittance payer differ only by case, whitespace, punctuation, and 32-char truncation that does **not** remove the distinguishing token (`ACME CORPORATION LTD` → `ACME CORPORATION LT` ok; `ACME CORPORATION` → `ACME` not ok).
- Dates: payment-side dates are `paid_on` or `paid_on + 1 business day`, and `paid_on ≤ due_date + 1 bd`.
- Amounts: payment record equals `base_amount_minor` exactly (direct or `pay_by_bank`). Card-paid events cannot be clean (§4.2).
- Reference: present per the base rates in §3.2/§3.3.

### 4.2 `fee` — 10%

Processor-paid, `product=card`. Settlement line exposes `gross/fee/net`; bank sees only the batch payout. Rules can resolve with the processor record; without it the asymmetric `FEE_WINDOW` applies. Fee events are the *only* card-product lines, so `fee_minor > 0 ⇔ fee` — acceptable, this class exists to prove the matcher uses a third source, not to be hard.

### 4.3 `timing` — 8%

Definition, sharp against `clean`:
- payment-side value/settlement date is **2–5 business days after `due_date`**, and
- for processor-paid events, `settled_on` additionally lags the remittance/`created_at` date by 2–5 business days.
- ≥50% of timing cases span a weekend or holiday so calendar-day arithmetic gives the wrong lag.

Lag measured in business days from `holidays_gb_eng.csv`. Clean events never exceed `due_date + 1 bd`, so there is no overlap.

### 4.4 `name` — 7%

Payer name on the payment side is drifted beyond trivial normalisation but still resolvable: legal-suffix swap (`LTD`/`LIMITED`/`PLC`/`& CO`), trading name from the ERP `trading_name` column, truncation that removes the distinguishing token, `(UK)`/`(HOLDINGS)` suffixes, one or two character typos. Every case is asserted `token_set_ratio(legal_or_trading_name, drifted) ≥ NAME_SIM_MIN` against at least one of legal/trading name. Amount and dates are clean.

### 4.5 `partial` — 5%

> **Session 1.3:** the instalment gaps are drawn *before* the invoice, so its issue date can
> reserve exactly the room the schedule needs. Choosing the invoice first and clamping
> instalments to the end of the payment window puts two instalments on the same day, and
> snapping a 45-day gap forward onto a business day overshoots the 45-day bound; both are
> caught by §10.4. A consequence: a counterparty on 60-day terms drawing three long gaps can
> only be invoiced in the first half of the year, so `partial` issue dates skew earlier than
> other classes'. The spec constrains the gaps, not the issue-date distribution.


One invoice, N ∈ {2, 3, 4} payment records summing **exactly** to `base_amount_minor`, spread 1–45 days apart, allowed to cross a month end. Always has a remittance PDF listing the instalments. Manifest: `source_ids=[erp:INV]`, `target_ids=[N payment records]`.

### 4.6 `consolidated` — 4%

One payment record covering N ∈ {2, 3, 4} invoices from the same counterparty, optionally (40%) less one credit note issued before `paid_on`. Sum is exact: `payment = Σ invoices − CN`. Always has a remittance PDF listing every component. Manifest: `source_ids=[erp:INV…, erp:CN?]`, `target_ids=[payment]`. N = 4 is the group-size cap; nothing in this class exceeds it.

> **Session 1.3:** the two bounds above conflict — four invoices *plus* a credit note is five
> source ids. Resolved in favour of the cap: the **source side never exceeds 4 ids**, so N is
> 2–4 invoices on its own and 2–3 when the credit-note variant fires. Downstream candidate
> budgets can rely on the cap. Also: a consolidated group's invoices are clustered within one
> period (≤30 days, and never more than the terms allow), because a payment that is on time for
> the earliest due date must not predate the latest invoice it settles.


### 4.7 `fx` — 3%

> **Session 1.3:** §4.7 settles processor-paid foreign events in USD/EUR, while §3.3 pays each
> batch out as one bank `TFR` line equal to `total_net_minor` — but the bank is a GBP account,
> so a foreign batch cannot satisfy both. Resolved by batching on **(settled_on, currency)**:
> a batch record gains `currency` and `payout_base_minor`, and the `TFR` line carries
> `payout_base_minor` = `total_net_minor` converted at the **payout-date** rate (equal to
> `total_net_minor` for GBP). The validator checks `total_net_minor = Σ net_minor` for every
> batch and the conversion for foreign ones. Settlement lines also gain `fx_rate` /
> `fx_rate_date` (null for GBP), which is where §4.7's "settlement-date rate recorded" lives.


Counterparty currency is USD or EUR. ERP stores the foreign amount and `base_amount_minor` at the **invoice-date** rate. Bank receives GBP converted at the **value-date** rate. Processor settles in the foreign currency with the **settlement-date** rate recorded. The residual is bounded by the walk over ≤5 days. The FX table is available to the matcher; this class is rules-solvable. Foreign currency implies `fx` — acceptable for the same reason as §4.2.

### 4.8 `reasoning` — 3%, three sub-types, ~100 each

Constructed to be unsolvable by fuzzy match or arithmetic, and **asserted** after generation with the shared thresholds. Any case that fails the assertion aborts generation.

- **(a) parent / subsidiary / DBA** — invoice issued to a subsidiary (`Google UK Ltd`), payer is the parent or DBA (`Alphabet Inc`) from the hidden registry. Assert `token_set_ratio < NAME_SIM_MIN` against both legal and trading name. Amount and dates clean. A remittance PDF, when present (40%), names the subsidiary in the narrative only.
- **(b) short-pay with narrative** — payment = gross − X, X in 5–40% of gross (outside `FEE_WINDOW`), remittance narrative says why and cites a credit note (`CN-2025-…`) that **does not exist in the dataset** (issued after the window). Only the PDF narrative resolves it. Assert `net/gross ∉ FEE_WINDOW`.

> **Session 1.3:** "5–40%" and `net/gross ∉ FEE_WINDOW` cannot both hold. `FEE_WINDOW` is
> `(0.90, 1.02)`, so a 5% short-pay gives 0.95 — inside the window, i.e. indistinguishable from
> a processor fee. The **assertion wins**, since being outside the fee window is the reason the
> class exists: X is drawn from **1050–4000 bp (10.5–40%)**, giving `net/gross ≤ 0.895`.
> Implemented as `types/reasoning.SHORT_PAY_BP`.
> The cited `CN-2025-*` is kept non-existent by clamping every generated credit note's
> `issued_on` to the invoice window (§4.6), which the validator asserts.

- **(c) consolidated with rounding residual** — as §4.6 but the payment differs from the exact sum by 1–5p, and the PDF's per-line amounts differ from ERP by the same pennies with a "rounding adjustment" note. Assert `0 < |residual| ≤ 5` minor units. Below any fee window, above `EXACT_SUM_TOL`; no rule the matcher is told about closes the gap.

Manifest carries `reasoning_subtype ∈ {dba, short_pay, rounding}`.

### 4.9 `duplicate` — 2%

The **payment record** is duplicated (bank line for direct, settlement line for processor-paid) so the class is independent of payment method. The copy is perturbed as a re-export would be: new record id, same amount and date, `posted_at`/`created_at` differs, description may re-truncate differently. The earlier id is canonical. Manifest: the event's group targets the canonical record; the copy is its own group `source_ids=[]`, `target_ids=[copy]`, `match_type=duplicate`, `duplicate_of=<canonical id>`.

### 4.10 `no_match` — 1% of events, plus noise records

Event orphans, 50/50:
- `kind=open_invoice` — ERP invoice with no payment anywhere: `source_ids=[erp:INV]`, `target_ids=[]`.
- `kind=unknown_receipt` — bank line (direct) with no invoice, no processor line, no PDF: `source_ids=[]`, `target_ids=[bank:…]`.

Noise records, outside the event quota but every one labelled:
- `kind=batch_payout` — every `TFR` bank line (§3.2).
- `kind=refund` — ~50 outbound `FPO` bank lines to counterparties, `Paid Out` populated.

The validator counts `no_match` by `kind`; only `open_invoice` + `unknown_receipt` count toward the 1%.

---

## 5. Manifest and scoring unit

### 5.1 Group shape — ERP-hub with heterogeneous target

- `source_ids` — ERP records: invoices and credit notes.
- `target_ids` — payment records: bank lines (direct) or processor settlement lines (processor-paid).
- `evidence_ids` — everything else tied to the event: remittance record, and for processor-paid events the batch payout bank line's batch id is in `notes`, not here (the payout line is its own `no_match` group).

Scoring identity is `(frozenset(source_ids), frozenset(target_ids))`. A predicted group is a true positive only if identical; partial overlap is one FP and one FN. `match_type` is compared separately as a secondary metric. `evidence_ids` never affect TP/FP. Orphans and duplicates are singleton groups with one empty side, so the confusion matrix has a no-match row and correct non-matches score.

### 5.2 File — `manifest.jsonl`

Line 1 is a header; every subsequent line is one group.

```json
{"schema_version":1,"seed":42,"generator_sha":"<git sha>","kind":"full","event_count":10000,"type_counts":{"clean":5700,…},"no_match_kinds":{"open_invoice":50,"unknown_receipt":50,"batch_payout":…,"refund":50}}
{"event_id":"evt:000001","match_type":"clean","source_ids":["erp:INV-2024-0001"],"target_ids":["bank:2024-01-16:000004"],"evidence_ids":[],"payment_method":"direct","paid_on":"2024-01-16","decoys":[],"notes":null}
{"event_id":"evt:000317","match_type":"consolidated","source_ids":["erp:INV-2024-0510","erp:INV-2024-0533","erp:CN-2024-0007"],"target_ids":["proc:stl_…"],"evidence_ids":["remit:RA-2024-0088"],"payment_method":"processor","paid_on":"2024-04-02","decoys":["round_amount"],"notes":"batch=bat_2024-04-03_01"}
{"event_id":"evt:000900","match_type":"reasoning","reasoning_subtype":"short_pay","source_ids":["erp:INV-2024-1402"],"target_ids":["bank:2024-06-11:000012"],"evidence_ids":["remit:RA-2024-0410"],"payment_method":"direct","paid_on":"2024-06-11","decoys":[],"notes":"cited CN-2025-0031 not in dataset"}
{"event_id":null,"match_type":"duplicate","duplicate_of":"bank:2024-06-11:000012","source_ids":[],"target_ids":["bank:2024-06-12:000003"],"evidence_ids":[],"notes":null}
{"event_id":null,"match_type":"no_match","kind":"batch_payout","source_ids":[],"target_ids":["bank:2024-04-04:000001"],"evidence_ids":[],"notes":"batch=bat_2024-04-03_01"}
```

`source_ids` and `target_ids` are written sorted. `paid_on`, `payment_method`, `decoys` and `notes` are analysis fields; the eval never reads them for scoring.

### 5.3 Record ids

`erp:INV-2024-NNNN`, `erp:CN-2024-NNNN`, `bank:YYYY-MM-DD:NNNNNN`, `proc:stl_<12 hex>`, `remit:RA-2024-NNNN`. Every id in every source file appears in **exactly one** group across `source_ids ∪ target_ids ∪ evidence_ids`; the validator enforces this both ways.

### 5.4 `MatchDecision` — system output

Unchanged from the plan; restated so the comparison is literal:

```
MatchDecision
  source_ids: list[str]
  target_ids: list[str]
  match_type: clean | fee | timing | name | partial | consolidated | fx | reasoning | duplicate | no_match
  action: match | flag | escalate
  evidence: list[{record_id, field, value}]
  score: Decimal
```

One function, `crossfoot.eval.compare.group_key(obj) -> tuple[frozenset[str], frozenset[str]]`, accepts both a manifest group and a `MatchDecision`. The Session 1.2 verification round-trips one of each through it.

### 5.5 Expected action is derived, not stored

The manifest never stores an expected `action`. The eval derives it from a policy table in eval config (initially: `reasoning → escalate` acceptable, all others `→ match`). Changing policy does not regenerate ground truth.

---

## 6. Remittance PDF templates

Four templates, chosen per counterparty (a payer always uses the same template) **[decided]**, with weights 30/30/25/15:

| Id | Layout |
|---|---|
| A | Portrait. Header block top-left, single line-item table, totals row. Refs `INV-2024-0891`. |
| B | Portrait. Two-column header (payer / payee), line items with running balance, narrative in footer. Refs `INV-2024-0891`. |
| C | Portrait, **multi-page** when > 25 lines. Column order amount-first, amounts right-aligned with `£`/`$`/`€`. Refs `INV-2024-0891`. |
| D | **Held out.** Landscape. Narrative *before* the table. Refs written `0891/2024`. Thousands separators. |

`HELD_OUT_TEMPLATE = "D"` is a constant in config, not seed-dependent. Parser development (Phase 2) never opens a D file; D's parse rate is reported at the end. Template id is in `remit.jsonl`, not on the PDF.

### 6.4 PDF presence must not leak the label

`partial`, `consolidated`, `reasoning(b)`, `reasoning(c)` always have a PDF. To keep overall PDF rate at ~40% and `has_pdf` uninformative for the other types, the generator gives `clean`/`fee`/`timing`/`name`/`fx`/`reasoning(a)` events a PDF at the rate that brings the total to 40% (~33% of those events). The validator asserts the per-type PDF rate is within ±3 pp for the non-mandatory types.

> **Session 1.3:** §10.6 words this as "±3 pp of the **global** rate", which is unsatisfiable —
> the global rate is 40% while the non-mandatory types sit at 33.7% by this section's own
> arithmetic, 6.3 pp away. The reference is the **pooled non-mandatory rate**, the only reading
> under which `has_pdf` is uninformative *between* those types. The top-up rate is derived from
> the natural rate table as `(40 − 11)/86 = 29/86`, not from the dataset in hand, so each
> bucket's count depends only on its own size and §9's per-type isolation holds. Measured at
> seed 42: every bucket within 0.3 pp of the pooled 33.7%, global 40.0%.
> `duplicate` and `no_match` carry no advice — an `unknown_receipt` with a remittance advice
> would not be unknown.
>
> The ~40% *global* figure holds for the full set (measured 40.0%) but not for a slice, which
> is 43.7% at `DEV_SEED`: the floor of 100 per type over-weights the always-advised classes
> relative to `clean`. The invariant that matters — the pooled non-mandatory rate, 33.5% on the
> slice — holds in both, and that is what §10.6 asserts.


---

## 7. Decoys (hard negatives)

Properties of otherwise-ordinary events, tagged in `decoys` for per-decoy precision reporting. Targets for the full set:

| Tag | Construction | Count |
|---|---|---|
| `amount_collision` | Two invoices from **different** counterparties with identical `base_amount_minor`, paid within ±3 business days | ~150 pairs |
| `near_amount` | Same counterparty, two invoices within 2% of each other, paid a day apart | ~150 pairs |
| `round_amount` | Amount is a multiple of £50 | ~10% of invoices |
| `recurring` | ~20 counterparties with a fixed monthly invoice for all 12 months; only the date discriminates | ~240 invoices |

Decoys are applied to `clean`, `timing` and `name` events only, so they never coincide with an arithmetic class and muddy its boundary. Slices carry decoys at the same proportions.

---

## 8. Seeds, slices and the golden directory

### 8.1 Datasets

| Kind | Command | Events | Type quotas | Location | Committed |
|---|---|---|---|---|---|
| full | `just seed 42` | 10,000 | natural rates (§4) | `data/full/` | no (gitignored) |
| dev slice | `just slice $DEV_SEED dev` | 1,200 | floor 100 per type + 200 at natural rate | `tests/fixtures/golden/dev/` via `just golden` | yes |
| test slice | `just slice $TEST_SEED test` | 1,200 | same rule | `data/test/` | no — **not generated until Phase 8** |

Slice quotas (floor 100 + 200 × natural rate): clean 214, fee 120, timing 116, name 114, partial 110, consolidated 108, fx 106, reasoning 106 (36/35/35 by sub-type), duplicate 104, no_match 102 (51/51). Clean remains the plurality so slice-level aggregates resemble the full set; per-type numbers are the headline anyway.

Slices are **independent worlds**: the generator runs end to end with the slice's seed and quotas and its own counterparty pool (~60). No record, counterparty or PDF is shared with the full set or the other slice.

### 8.2 Seeds

`src/crossfoot/generate/seeds.py` holds `DEV_SEED` and `TEST_SEED` as constants **[decided]**. `just slice $TEST_SEED` and `just eval-test` refuse to run unless `CROSSFOOT_ALLOW_TEST=1`. Nothing else in the repo reads `TEST_SEED`.

### 8.3 Golden gate

`tests/fixtures/golden/` is ground truth. The Write/Edit hook already blocks tool writes. The generator itself refuses to write under that path unless `CROSSFOOT_REGEN_GOLDEN=1`. `just golden` sets the flag, regenerates `golden/dev/`, prints `git diff --stat`, and exits non-zero if the tree is dirty and no commit message was supplied — a regeneration is always a deliberate, reviewed commit.

---

## 9. Determinism

- Master `random.Random(seed)` only allocates quotas and ordering. Each event `i` gets its own `random.Random(int.from_bytes(blake2b(f"{seed}:{i}".encode(), digest_size=8).digest(), "big"))`. Python's built-in `hash()` is never used (string hashing is salted per process).
- Changing the generator for one type must not change any other type's records for the same seed. A test pins the dev-slice hashes per source file and per type.
- All ids derive from `(seed, index)`; no `uuid4`, no wall clock, no `datetime.now()`.
- Every output file is written in a fixed order (sorted keys, sorted ids, `\n` or CRLF as specified, no trailing whitespace). JSON is written with `sort_keys=True, separators=(",", ":")`.
- PDFs: reportlab `invariant=1`, no `/CreationDate` drift, built-in Type 1 fonts only.

> **Session 1.3, measured not assumed:** `invariant` does **not** omit `/ID`; it derives the
> array from a digest of the document content, so it is stable for the same input. Two runs in
> separate processes then agree byte for byte.
> `/Producer` and `/Creator` default to version-dependent strings and are overridden with our
> own. What remains version-dependent is reportlab's `%PDF` header banner comment, which no
> public API controls: measured across 4.2.5 and 5.1.0 the object bodies are **identical**, and
> only that banner (12 bytes) and the xref offsets it shifts differ. Since
> `tests/fixtures/golden/dev/` commits ~480 advices as bytes, **reportlab is pinned exactly**
> in `pyproject.toml` and `test_remit.py` asserts the pin, so an upgrade is a deliberate golden
> regeneration rather than a transitive resolution.

- Verification: `just seed 42` twice → `just seed-check` compares a SHA-256 over every file under `data/full/` and fails on any difference.

---

## 10. Validator — `crossfoot.generate.validate`

`just validate data/full` runs on any dataset directory and asserts, printing a per-type table:

1. **Counts** — `type_counts` in the header equal the header's declared quotas exactly; `no_match` broken down by `kind`; reasoning by sub-type; slices ≥100 per type.
2. **Referential integrity** — every id in every source file appears in exactly one group; every id in the manifest exists in a source file; `duplicate_of` targets exist and are not themselves duplicates.
3. **Arithmetic** — `partial` and `consolidated` sums exact; `fee` has `gross − fee = net` and `net/gross ∈ FEE_WINDOW`; `fx` reproduces from the rate table on the stated dates; `rounding` residual in 1–5 minor units; `short_pay` ratio outside `FEE_WINDOW`; batch `total_net_minor = Σ net_minor` and equals the `TFR` bank line.
4. **Dates** — `clean` payment dates ≤ `due + 1 bd`; `timing` in `due + 2..5 bd` with ≥50% spanning a non-business day; all business-day arithmetic against `holidays_gb_eng.csv`.
5. **Names** — `name` cases ≥ `NAME_SIM_MIN`; `reasoning(a)` cases `< NAME_SIM_MIN` on both legal and trading name; `clean` cases pass trivial normalisation.
6. **Leakage** — per-type PDF presence within ±3 pp of the global rate for non-mandatory types; decoys only on `clean`/`timing`/`name`.
7. **Money and dates** — no float anywhere in generator source (hook) and no non-integer minor units in any emitted file; calendar-date fields match `^\d{4}-\d{2}-\d{2}$` or `DD/MM/YYYY` in the bank CSV only.

The Session 1.3 verification is this script's output over `data/full` and over the dev slice.

---

## 11. Package layout and recipes

> Command form corrected in Session 1.2: `just` takes recipe arguments positionally, so the
> recipes are `just seed 42`, `just seed-check 42`, `just validate data/full` (each argument
> has a default), not the `NAME=value` shorthand written above.

```
src/crossfoot/generate/
  __init__.py
  seeds.py          # DEV_SEED, TEST_SEED
  world.py          # counterparties, parent registry, calendars, fx walk
  events.py         # quota allocation, per-event substreams, type dispatch
  types/            # one module per discrepancy type, each: build(event_rng, world) -> Event
  emit/erp.py  emit/bank.py  emit/processor.py  emit/remit.py   # source writers
  manifest.py       # group construction + writer
  validate.py       # §10
  wordlists/        # name grammar inputs, narrative corpus
src/crossfoot/config.py   # Thresholds, HELD_OUT_TEMPLATE
```

justfile recipes added in 1.2/1.3: `seed`, `seed-check`, `slice`, `validate`, `golden`. `just eval-test` (Phase 3) carries the `CROSSFOOT_ALLOW_TEST` guard.

Dependencies added: `reportlab` (generator only), `rapidfuzz` (shared thresholds). No Faker, no numpy in the generator.

---

## 12. Session plan

- **1.2** — `config.py`, `seeds.py`, world + calendars + FX walk, event/quota engine with substreams, `clean` + `fee` + `timing`, ERP/bank/processor writers, manifest writer, `group_key`, `just seed` / `seed-check`. Verification: byte-identical double run; manifest round-trips the validator's integrity checks; a manifest group and a `MatchDecision` round-trip `group_key`.
- **1.3** — remaining seven types, decoys, remittance PDFs (four templates; delegate the reportlab layout exploration to a subagent), `remit.jsonl`, `just slice`, `just golden`, full validator. Verification: validator output over `data/full` and `golden/dev`, all assertions in §10 green; `data/test` never generated.

---

## 13. Circularity disclosure (for README)

The generator and the matcher share an author and share `Thresholds`. Deterministic coverage on this data is therefore an **upper bound** on real-world coverage. The data is harder than the rules know in exactly three places: the `reasoning` class (asserted unsolvable by the shared thresholds), the held-out PDF template D, and the decoys in §7. Nothing else should be presented as evidence of generalisation.

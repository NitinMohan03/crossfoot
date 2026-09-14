---
name: recon-domain
description: Financial reconciliation domain knowledge — settlement lag, fee deduction, remittance advice structure, partial vs consolidated payments, entity-name drift. Load when writing matching rules, blocking queries, discrepancy generators, or LLM prompts that reason about why two records differ.
---

# Reconciliation domain notes

## The four sources describe one event differently
One commercial event (Acme pays invoice 0891) appears as one record per source, and no two agree:
- **ERP** (Postgres): invoice `INV-2024-0891`, legal name "Acme Corporation", gross amount, issue date.
- **Bank** (fixed-width/CSV): net amount landed, value date, 32-char truncated description ("ACME CORP LT"), no invoice ref.
- **Processor** (REST): settlement batch closing 1 day after bank, one line net of fee, batch total across ~50 payments.
- **Remittance** (PDF): payer's own statement — which invoices this payment covers, credit notes applied, free-text notes.

No shared identifier links them. Matching is inference over amount, date, name, and reference.

## Why amounts differ
- **Fee deduction**: processor takes a fee; bank sees net. Fees *deduct* — net is below gross, never above. Blocking window is asymmetric: `amount BETWEEN gross*0.90 AND gross*1.02`.
- **FX rounding**: cross-currency pairs use rates applied on different days by each system. Small residual after converting with the daily rate table.
- **Partial payment**: one invoice, N bank lines summing to it (possibly across periods).
- **Consolidated payment**: one bank line covering N invoices, sometimes less a credit note. Remittance PDF is the only source that lists the components.
- **Short-pay with reason**: remittance says "short-paid, damaged goods, CN-12 next month". Arithmetic alone cannot resolve — reasoning-required class.

Money is `Decimal` or integer minor units. Python verifies every balance; the model never does arithmetic.

## Why dates differ
- Transaction date → value date → settlement date → posting date. Settlement lag is 1–5 business days.
- Weekends and bank holidays shift dates. One jurisdiction's holiday calendar (generated in Phase 1, named in README) drives business-day windows: `business_days(date, -1)` to `business_days(date, +5)`.
- **Calendar dates stay calendar dates.** Converting a value date to UTC can move it a day. Only timestamps go to UTC (original kept).

## Why names differ
"ACME CORP LTD" / "Acme Corporation" / "ACME CORP LT" (truncated) / "Acme Corp (UK)" / "Alphabet" paying a "Google" invoice (parent/DBA).
- Fuzzy layer: `pg_trgm` similarity for blocking, RapidFuzz `token_set_ratio` for ranking.
- Alias registry learns **only from human-confirmed escalations**, frozen during eval runs. Learning from the manifest is leakage; from LLM decisions is compounding error.
- Parent/subsidiary/DBA cases are reasoning-required — fuzzy matching cannot bridge "Alphabet" ↔ "Google".

## Remittance advice
Structured document: header (payer, payment ref, date, total) + line items (invoice ref, amount, optional note) + optional free-text narrative. **Parse structurally with pdfplumber; never chunk semantically** — a line item is meaningless without the header naming the payer. 3–4 layout templates exist; one is held out from parser development. Remittance narrative is the only genuine free text in the system and the only place hybrid retrieval (BM25 + dense) is tested.

## Decision asymmetry
A false match silently corrupts the ledger. A missed match lands in a review queue. **Tune for precision.** The agent has a natural "I don't know": escalate to human. Escalation is the only write path.

## Bounds (deliberate)
- Many-to-many group size capped at 4; beyond that, escalate.
- LLM sees ≤10 candidates, ≤3 tool calls, ≤4 steps, then must decide.

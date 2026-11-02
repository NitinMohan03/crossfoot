---
name: fixture-conventions
description: Ground-truth manifest format, discrepancy-type labels and target rates, MatchDecision output schema, seeding and dev/test slice rules for crossfoot's synthetic data. Load when working on the generator, manifest, eval slices, tests under tests/fixtures/, or anything that reads or emits MatchDecision.
---

# Fixture conventions

The authoritative spec is `specs/01-data-generation.md`. **Read it before changing anything under
`src/crossfoot/generate/`** -- it supersedes this file wherever the two disagree. What follows is the
quick reference.

Implementation status: Session 1.2 built `clean`, `fee` and `timing` plus the ERP/bank/processor
writers, the manifest and `just seed` / `seed-check` / `validate`. The other seven types, the decoy
set, the remittance PDFs and the slice/golden machinery are Session 1.3, and
`crossfoot.generate.validate.PENDING` lists the checks still stubbed out.

## Vocabulary
- **Event** — one commercial fact (Acme pays invoice 0891).
- **Record** — one row in one source. A clean event = one record per source. A partial payment = one event, one invoice record, N bank records.
- Rates below are **per event**.

## Discrepancy types and target rates (10,000 events)

| Label | Rate | Count | Notes |
|---|---|---|---|
| `clean` | 57% | 5,700 | Rules alone should catch |
| `fee` | 10% | 1,000 | Net below gross by a small variable amount |
| `timing` | 8% | 800 | Settlement lag 1–5 business days |
| `name` | 7% | 700 | Legal / trading / truncated / suffixed |
| `partial` | 5% | 500 | One invoice, N payments |
| `consolidated` | 4% | 400 | One payment, N invoices, optional credit note |
| `fx` | 3% | 300 | Cross-currency, rate applied on different days |
| `reasoning` | 3% | 300 | DBA/subsidiary names, remittance free-text short-pay, consolidated + rounding. **Not solvable by fuzzy match or arithmetic.** |
| `duplicate` | 2% | 200 | Same transaction ingested twice; carries `duplicate_of` |
| `no_match` | 1% | 100 | Orphan: present in one source, absent elsewhere |

Every record is labeled with its type so eval reports per type. Counts must be within tolerance of target — a validator script asserts this.

## Manifest
Machine-readable. Unit is the **match group**: `{source record ids} → {target record ids}` plus `match_type`. Orphans have explicit `no_match`; duplicates have `duplicate_of: <record id>`. The manifest and `MatchDecision` round-trip through the **same comparison function**.

## MatchDecision (system output, directly comparable to manifest)
```
MatchDecision
  source_ids: list[str]
  target_ids: list[str]
  match_type: clean | fee | timing | name | partial | consolidated | fx | reasoning | duplicate | no_match
  action: match | flag | escalate
  evidence: list[{record_id, field, value}]   # cited, never free text
  score: Decimal                               # computed in Python from evidence features, never by the model
```

## Determinism and seeds
- `just seed SEED=42` twice → byte-identical output. Any nondeterminism is a bug.
- **Dev slice = seed A**, **test slice = seed B**. Each ~1,000 events, **stratified to ≥100 per type** regardless of natural rate.
- Test slice seed is recorded and never run before Phase 8. `just eval-test` requires `CROSSFOOT_ALLOW_TEST=1`.

## Companion tables (generated alongside)
- Daily FX rate table, three currencies.
- Holiday calendar, one jurisdiction (UK or US — pick once, state in README).
- 3–4 remittance PDF layout templates via reportlab; **one held out** from parser development, used only to report parse rate.

## Money and dates in fixtures
- Amounts as `Decimal` / integer minor units. Never float, in generator or fixtures.
- Timestamps UTC with original kept. Value/posting dates are calendar dates — never timezone-shifted.

## tests/fixtures/golden/
Ground truth. A PreToolUse hook blocks Write/Edit there. If a test disagrees with golden, the code is wrong, not the fixture. Regenerate golden only via the seeded generator, deliberately, with the diff reviewed.

## Circularity (say it in README)
Generator and matcher share an author, so deterministic coverage on synthetic data is an **upper bound** on real-world coverage. The `reasoning` class and the held-out PDF template are the only places the data is harder than the rules know.

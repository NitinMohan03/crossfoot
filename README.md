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

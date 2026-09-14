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

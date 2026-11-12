# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Commands
- Setup: `just setup` (= `docker compose up -d --wait && uv sync`). Python 3.12 pinned; never use system 3.14.
- All checks: `just check` (ruff, ruff format --check, mypy strict, pytest). Run before saying anything is done.
- Single test: `uv run pytest tests/test_smoke.py::test_package_imports`
- DB: `just db-shell` (psql), `just db-reset` (wipe volume). Extensions `vector` + `pg_trgm` load from `docker/initdb/`. Host port is **5433** (a native Windows postgres often holds 5432).
- `just db-ref data/full` loads the FX rate and bank-holiday tables into the `ref` schema.
- `just db-er` creates the alias registry table; `just db-roles` creates the read-only role the
  MCP servers connect as. `just db-load` re-applies the grants, because the ERP dump drops its
  schema and takes them with it.
- `just mcp-erp` serves the ERP over MCP on stdio.
- `just proc-api data/full 8099` serves the mock payment processor REST API.
- Eval/cost recipes do not exist yet — added per phase in `reconciliation-agent-plan.md` Part 4.

# Architecture
- Deterministic rules run FIRST (`src/crossfoot/matching/`). LLM only sees what rules can't resolve.
- Every source normalises into `src/crossfoot/models/canonical.py`. A new source adds a `detail`
  variant; the envelope does not change.
- MCP servers: `src/crossfoot/mcp/__init__.py` states the pattern, `mcp/erp/` is the reference.
  Every tool read-only, enforced at the DB role and the transaction, not by convention.
- Entity resolution is `src/crossfoot/entities/`, not `matching/`. The alias registry learns only
  from human-confirmed escalations and is frozen during eval runs.
- Money is Decimal or integer minor units. Never float. No exceptions.
- Timestamps → UTC at ingest, original kept. Calendar dates stay dates — never convert a date to UTC.
- LLM emits decision + cited evidence. Score is computed in Python. Model never does arithmetic.
- Candidate generation is blocking (`src/crossfoot/blocking/`: SQL tolerance + pg_trgm + RapidFuzz), not retrieval.

# Workflow
- IMPORTANT: any change under `src/crossfoot/matching/` or `src/crossfoot/blocking/` requires `just eval-fast` (once it exists) before done.
- Never edit `tests/fixtures/golden/`. It is ground truth.
- Show test output as evidence. Don't assert that something passes.

# Budget
- Default model is Haiku. Sonnet requires explicit instruction.
- Every LLM call goes through `src/crossfoot/llm/`. Never call the API directly elsewhere.
- LLM sees ≤10 candidates, ≤4 steps. Changing either changes the budget — ask.

# Git
- IMPORTANT: never add Co-Authored-By, "Generated with Claude Code", or any attribution trailer to commit messages or PR descriptions.

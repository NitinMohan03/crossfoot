set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]
set dotenv-load

# List recipes
default:
    @just --list

# Start Postgres and install the Python environment
setup: db-up
    uv sync

# Run tests
test:
    uv run pytest

# Lint
lint:
    uv run ruff check .
    uv run ruff format --check .

# Format and autofix
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Type-check
typecheck:
    uv run mypy

# Lint + typecheck + test
check: lint typecheck test

# Start Postgres (pgvector + pg_trgm) and wait until healthy
db-up:
    docker compose up -d --wait

# Stop Postgres, keep data
db-down:
    docker compose down

# Stop Postgres and wipe data volume
db-reset:
    docker compose down -v
    docker compose up -d --wait

# psql into the database
db-shell:
    docker compose exec db psql -U crossfoot -d crossfoot

# Generate the full dataset (spec 01 §8.1)
seed SEED="42":
    uv run python -m crossfoot.generate --seed {{SEED}} --kind full --out data/full

# Prove a second run is byte-identical
seed-check SEED="42":
    uv run python -m crossfoot.generate --seed {{SEED}} --kind full --out data/.seedcheck
    uv run python -m crossfoot.generate.hashes data/full data/.seedcheck

# Assert the ground-truth invariants over a dataset directory
validate DIR="data/full":
    uv run python -m crossfoot.generate.validate {{DIR}}

# Load a generated ERP dump into Postgres (Phase 2 does this properly)
db-load DIR="data/full":
    Get-Content {{DIR}}/erp.sql | docker compose exec -T db psql -U crossfoot -d crossfoot -v ON_ERROR_STOP=1

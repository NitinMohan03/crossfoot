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

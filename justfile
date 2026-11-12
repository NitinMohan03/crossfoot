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

# Generate an independent eval slice into data/<NAME> (spec 01 §8.1)
slice SEED NAME:
    uv run python -m crossfoot.generate --seed {{SEED}} --kind slice --out data/{{NAME}}

# Regenerate the committed dev slice. Needs a message when it changes (spec 01 §8.3)
golden MSG="":
    $env:CROSSFOOT_REGEN_GOLDEN = "1"; uv run python -m crossfoot.generate.golden {{MSG}}

# Assert the ground-truth invariants over a dataset directory
validate DIR="data/full":
    uv run python -m crossfoot.generate.validate {{DIR}}

# Serve the generated settlement data as a mock processor REST API (plan §4.4)
proc-api DIR="data/full" PORT="8099":
    uv run python -m crossfoot.mock_processor --data {{DIR}} --port {{PORT}}

# Load the FX rate and bank-holiday tables into Postgres (plan §4.4)
db-ref DIR="data/full":
    uv run python -m crossfoot.db load-reference --dir {{DIR}}

# Create the `er` schema: the alias registry (plan §2.7)
db-er:
    uv run python -m crossfoot.db apply-er

# Create the read-only role the MCP servers connect as (plan §3.3). Needs a privileged user.
db-roles:
    uv run python -m crossfoot.db apply-roles

# Serve the ERP over MCP on stdio (plan §4.4). Reads as the crossfoot_ro role.
mcp-erp SCHEMA="erp":
    uv run python -m crossfoot.mcp.erp --schema {{SCHEMA}}

# Load a generated ERP dump into Postgres. The dump drops schema `erp`, which takes the
# read-only role's grants with it, so they are re-applied here -- without this the no-write
# test would pass for the wrong reason.
db-load DIR="data/full":
    Get-Content {{DIR}}/erp.sql | docker compose exec -T db psql -U crossfoot -d crossfoot -v ON_ERROR_STOP=1
    uv run python -m crossfoot.db apply-roles

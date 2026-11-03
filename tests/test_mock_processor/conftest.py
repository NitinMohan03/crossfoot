"""The API under test, serving the committed dev slice.

`tests/fixtures/golden/dev/` is ground truth and read-only; serving it means the pagination
assertions compare against bytes that are themselves committed, not against a fresh generation.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossfoot.mock_processor import ApiSettings, create_app

DEV_SLICE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "dev"
TOKEN = "test-token"
# What the fixture holds. Hardcoded so a change to the slice shows up here as a failure.
SETTLEMENT_COUNT = 552
BATCH_COUNT = 268


def raw_lines(name: str) -> list[str]:
    """Every non-blank line of a fixture file, exactly as the generator wrote it."""
    path = DEV_SLICE / "processor" / name
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def expected(name: str, date_field: str) -> list[dict[str, Any]]:
    """The fixture file parsed and ordered the way the API orders it."""
    records = [json.loads(line) for line in raw_lines(name)]
    return sorted(records, key=lambda record: (record[date_field], record["id"]))


@pytest.fixture(scope="session")
def settings() -> ApiSettings:
    # A burst far above any test's request count: only test_rate_limit.py wants a 429.
    return ApiSettings(data_dir=DEV_SLICE, token=TOKEN, rate_per_second=1000, burst=100_000)


@pytest.fixture(scope="session")
def client(settings: ApiSettings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}

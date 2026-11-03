"""Paginating the full set returns exactly the generated records (plan §4.4 verification)."""

import json
from typing import Any

from fastapi.testclient import TestClient

from tests.test_mock_processor.conftest import (
    BATCH_COUNT,
    SETTLEMENT_COUNT,
    expected,
    raw_lines,
)

# A walk must terminate; this is the tripwire if `next_cursor` ever stops advancing.
MAX_PAGES = 2_000


def walk(
    client: TestClient,
    auth: dict[str, str],
    path: str,
    limit: int,
    **params: str,
) -> tuple[list[dict[str, Any]], int]:
    """Follow `next_cursor` to the last page. Returns the records and the page count."""
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        query: dict[str, Any] = {"limit": limit, **params}
        if cursor is not None:
            query["cursor"] = cursor
        response = client.get(path, params=query, headers=auth)
        assert response.status_code == 200, response.text
        payload = response.json()
        # `has_more` and `next_cursor` must never disagree: a client may trust either.
        assert payload["has_more"] is (payload["next_cursor"] is not None)
        assert len(payload["data"]) <= limit
        records.extend(payload["data"])
        pages += 1
        assert pages < MAX_PAGES, "pagination did not terminate"
        cursor = payload["next_cursor"]
        if cursor is None:
            return records, pages


def test_settlement_walk_returns_every_generated_record(
    client: TestClient, auth: dict[str, str]
) -> None:
    records, pages = walk(client, auth, "/v1/settlements", limit=50)
    assert len(records) == SETTLEMENT_COUNT
    assert len({record["id"] for record in records}) == SETTLEMENT_COUNT
    assert records == expected("settlements.jsonl", "settled_on")
    assert pages == -(-SETTLEMENT_COUNT // 50)


def test_batch_walk_returns_every_generated_record(
    client: TestClient, auth: dict[str, str]
) -> None:
    records, _ = walk(client, auth, "/v1/batches", limit=50)
    assert len(records) == BATCH_COUNT
    assert records == expected("batches.jsonl", "closed_on")


def test_a_page_of_one_still_reaches_the_last_record(
    client: TestClient, auth: dict[str, str]
) -> None:
    records, pages = walk(client, auth, "/v1/batches", limit=1)
    assert pages == BATCH_COUNT
    assert records == expected("batches.jsonl", "closed_on")


def test_a_limit_above_the_total_is_one_page_with_no_cursor(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.get("/v1/settlements", params={"limit": 500}, headers=auth)
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["data"]) == 500
    assert payload["next_cursor"] is not None

    response = client.get("/v1/batches", params={"limit": 500}, headers=auth)
    payload = response.json()
    assert len(payload["data"]) == BATCH_COUNT
    assert payload["next_cursor"] is None
    assert payload["has_more"] is False


def test_records_are_served_byte_for_byte_as_generated(
    client: TestClient, auth: dict[str, str]
) -> None:
    by_id = {json.loads(line)["id"]: line for line in raw_lines("settlements.jsonl")}
    response = client.get("/v1/settlements", params={"limit": 5}, headers=auth)
    for record in response.json()["data"]:
        assert by_id[record["id"]] in response.text


def test_a_date_window_matches_a_python_side_filter(
    client: TestClient, auth: dict[str, str]
) -> None:
    lo, hi = "2024-06-01", "2024-06-30"
    records, _ = walk(
        client,
        auth,
        "/v1/settlements",
        limit=25,
        settled_from=lo,
        settled_to=hi,
    )
    wanted = [r for r in expected("settlements.jsonl", "settled_on") if lo <= r["settled_on"] <= hi]
    assert wanted, "fixture has no June 2024 settlements; pick another window"
    assert records == wanted


def test_single_record_endpoints_answer_from_the_same_bytes(
    client: TestClient, auth: dict[str, str]
) -> None:
    first = raw_lines("settlements.jsonl")[0]
    record_id = json.loads(first)["id"]
    response = client.get(f"/v1/settlements/{record_id}", headers=auth)
    assert response.status_code == 200
    assert response.text == first

    batch_line = raw_lines("batches.jsonl")[0]
    batch_id = json.loads(batch_line)["id"]
    assert client.get(f"/v1/batches/{batch_id}", headers=auth).text == batch_line

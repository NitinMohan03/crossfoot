"""A cursor the client did not get from us is rejected, and bad input is a 4xx, not a 500."""

import base64
import json
from datetime import date

from fastapi.testclient import TestClient

from crossfoot.mock_processor.cursors import encode
from tests.test_mock_processor.conftest import raw_lines


def first_cursor(client: TestClient, auth: dict[str, str], path: str) -> str:
    cursor = client.get(path, params={"limit": 1}, headers=auth).json()["next_cursor"]
    assert isinstance(cursor, str)
    return cursor


def test_a_real_cursor_works(client: TestClient, auth: dict[str, str]) -> None:
    cursor = first_cursor(client, auth, "/v1/settlements")
    response = client.get("/v1/settlements", params={"limit": 1, "cursor": cursor}, headers=auth)
    assert response.status_code == 200


def test_a_tampered_cursor_is_400(client: TestClient, auth: dict[str, str]) -> None:
    cursor = first_cursor(client, auth, "/v1/settlements")
    flipped = ("A" if cursor[0] != "A" else "B") + cursor[1:]
    response = client.get("/v1/settlements", params={"limit": 1, "cursor": flipped}, headers=auth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_cursor"


def test_a_truncated_cursor_is_400(client: TestClient, auth: dict[str, str]) -> None:
    cursor = first_cursor(client, auth, "/v1/settlements")
    response = client.get(
        "/v1/settlements", params={"limit": 1, "cursor": cursor[:-6]}, headers=auth
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_cursor"


def test_nonsense_is_not_a_cursor(client: TestClient, auth: dict[str, str]) -> None:
    for junk in ("", "!!!!", "Zm9v", "0"):
        response = client.get("/v1/settlements", params={"limit": 1, "cursor": junk}, headers=auth)
        assert response.status_code == 400, junk


def test_a_batches_cursor_is_not_valid_on_settlements(
    client: TestClient, auth: dict[str, str]
) -> None:
    """The cursor is scoped to its collection, so a client cannot cross the wires."""
    cursor = first_cursor(client, auth, "/v1/batches")
    response = client.get("/v1/settlements", params={"limit": 1, "cursor": cursor}, headers=auth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_cursor"


def test_a_cursor_cannot_be_forged_without_the_key(
    client: TestClient, auth: dict[str, str]
) -> None:
    """`encode` here uses the real key, which is exactly why a hand-built payload fails."""

    real = client.get("/v1/settlements", params={"limit": 1}, headers=auth).json()
    last = real["data"][-1]
    forged = (
        base64.urlsafe_b64encode(f"settlements|{last['settled_on']}|{last['id']}|0000".encode())
        .decode()
        .rstrip("=")
    )
    response = client.get("/v1/settlements", params={"limit": 1, "cursor": forged}, headers=auth)
    assert response.status_code == 400

    # The same key, used properly, is accepted -- so the rejection above is the digest, not luck.

    good = encode("settlements", (date.fromisoformat(last["settled_on"]), last["id"]))
    assert (
        client.get("/v1/settlements", params={"limit": 1, "cursor": good}, headers=auth).status_code
        == 200
    )


def test_an_unknown_id_is_404(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/settlements/proc:stl_nope", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_real_id_from_the_other_collection_is_404(
    client: TestClient, auth: dict[str, str]
) -> None:
    batch_id = json.loads(raw_lines("batches.jsonl")[0])["id"]
    assert client.get(f"/v1/settlements/{batch_id}", headers=auth).status_code == 404


def test_an_out_of_range_limit_is_422(client: TestClient, auth: dict[str, str]) -> None:
    for limit in (0, -1, 10_000):
        response = client.get("/v1/settlements", params={"limit": limit}, headers=auth)
        assert response.status_code == 422, limit
        assert response.json()["error"]["code"] == "invalid_limit"


def test_an_unparseable_date_is_422(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/settlements", params={"settled_from": "not-a-date"}, headers=auth)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_query"

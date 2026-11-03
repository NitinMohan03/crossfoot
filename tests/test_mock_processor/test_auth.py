"""Bearer-token auth: missing, malformed and wrong are three distinct failures."""

from fastapi.testclient import TestClient

from tests.test_mock_processor.conftest import TOKEN


def test_a_missing_header_is_401_with_a_challenge(client: TestClient) -> None:
    response = client.get("/v1/settlements")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "missing_token"


def test_a_non_bearer_scheme_is_401(client: TestClient) -> None:
    response = client.get("/v1/settlements", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_scheme"


def test_bearer_with_no_token_is_401(client: TestClient) -> None:
    response = client.get("/v1/settlements", headers={"Authorization": "Bearer"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_scheme"


def test_the_wrong_token_is_401(client: TestClient) -> None:
    response = client.get("/v1/settlements", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_a_near_miss_token_is_401(client: TestClient) -> None:
    response = client.get("/v1/settlements", headers={"Authorization": f"Bearer {TOKEN}x"})
    assert response.status_code == 401


def test_the_scheme_is_case_insensitive(client: TestClient) -> None:
    response = client.get("/v1/settlements", headers={"Authorization": f"bearer {TOKEN}"})
    assert response.status_code == 200


def test_the_right_token_is_200(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/v1/settlements", headers=auth).status_code == 200


def test_healthz_needs_no_token(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_auth_is_checked_before_the_record_is_looked_up(client: TestClient) -> None:
    """An unauthenticated probe must not be able to tell a real id from a fake one."""
    response = client.get("/v1/settlements/proc:stl_does_not_exist")
    assert response.status_code == 401

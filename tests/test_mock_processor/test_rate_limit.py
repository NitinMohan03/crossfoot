"""429 with a `Retry-After` that is worth obeying.

The Phase 2.2 client will sleep for whatever this header says, so the test asserts the promise:
waiting less is still refused, waiting that long succeeds. A fake clock makes that exact rather
than timing-dependent.
"""

from fastapi.testclient import TestClient

from crossfoot.mock_processor import ApiSettings, create_app
from tests.test_mock_processor.conftest import DEV_SLICE, TOKEN

AUTH = {"Authorization": f"Bearer {TOKEN}"}
BURST = 2
RATE = 1


class FakeClock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build() -> tuple[TestClient, FakeClock]:
    clock = FakeClock()
    settings = ApiSettings(
        data_dir=DEV_SLICE, token=TOKEN, rate_per_second=RATE, burst=BURST, default_limit=1
    )
    return TestClient(create_app(settings, clock=clock)), clock


def test_the_burst_is_spent_then_the_next_call_is_429() -> None:
    client, _ = build()
    for _ in range(BURST):
        assert client.get("/v1/settlements", headers=AUTH).status_code == 200

    response = client.get("/v1/settlements", headers=AUTH)
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "rate_limited"
    retry_after = int(response.headers["Retry-After"])
    assert retry_after >= 1
    assert error["retry_after"] == retry_after


def test_waiting_the_advertised_time_actually_works() -> None:
    client, clock = build()
    for _ in range(BURST):
        client.get("/v1/settlements", headers=AUTH)
    refused = client.get("/v1/settlements", headers=AUTH)
    retry_after = int(refused.headers["Retry-After"])

    # A hair short of the promise: still refused, and the header is still honest.
    clock.advance(retry_after - 0.01)
    short = client.get("/v1/settlements", headers=AUTH)
    assert short.status_code == 429
    assert int(short.headers["Retry-After"]) >= 1

    clock.advance(0.02)
    assert client.get("/v1/settlements", headers=AUTH).status_code == 200


def test_the_bucket_refills_at_the_configured_rate() -> None:
    client, clock = build()
    for _ in range(BURST):
        client.get("/v1/settlements", headers=AUTH)
    assert client.get("/v1/settlements", headers=AUTH).status_code == 429

    clock.advance(BURST / RATE)
    for _ in range(BURST):
        assert client.get("/v1/settlements", headers=AUTH).status_code == 200
    assert client.get("/v1/settlements", headers=AUTH).status_code == 429


def test_healthz_is_never_rate_limited() -> None:
    client, _ = build()
    for _ in range(BURST + 5):
        client.get("/v1/settlements", headers=AUTH)
    for _ in range(10):
        assert client.get("/healthz").status_code == 200


def test_an_unauthenticated_caller_gets_401_not_429() -> None:
    """Auth runs first, so a wrong token can never exhaust the real token's bucket."""
    client, _ = build()
    wrong = {"Authorization": "Bearer wrong"}
    for _ in range(BURST + 5):
        assert client.get("/v1/settlements", headers=wrong).status_code == 401
    assert client.get("/v1/settlements", headers=AUTH).status_code == 200

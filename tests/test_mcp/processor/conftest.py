"""The committed dev slice's settlement data, behind the real mock API, read by `mcp-processor`.

Three ways of standing in for the network, because three different things are being tested:

* `AppTransport` is the real FastAPI app from session 2.0, called in process. Its auth, its
  cursors, its rate limiter and its error envelope are the genuine ones; only the socket is
  missing. A `FakeClock` drives the limiter, so a 429 and the wait that clears it are exact.
* `ScriptedTransport` answers with whatever the test wrote down -- for the bodies and statuses
  the real API would never send, which is the point of testing them.
* `stub` is a real HTTP server on a real socket, for the one module that owns the socket.

The slice is ground truth and is only ever read.
"""

import json
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from crossfoot.mcp.processor.http import Reply, SourceDown
from crossfoot.mcp.processor.settings import ProcessorSettings
from crossfoot.mock_processor import ApiSettings, create_app
from tests.test_mcp.conftest import DEV_SLICE

PROCESSOR_DIR = DEV_SLICE / "processor"
TOKEN = "test-processor-token"
#: Starlette's test client answers for any host; this one is as good as another.
BASE_URL = "http://testserver"
#: A DSN nothing listens on, for a service that must never reach for the database.
NO_DATABASE = "postgresql://crossfoot_ro:crossfoot_ro@127.0.0.1:1/crossfoot"

# What the committed slice holds. Hardcoded so a change to the slice shows up here as a failure.
SETTLEMENTS = 552
BATCHES = 268
BY_PRODUCT = {"pay_by_bank": 432, "card": 120}
BY_CURRENCY = {"GBP": 479, "USD": 46, "EUR": 27}
BATCHES_BY_CURRENCY = {"GBP": 202, "USD": 41, "EUR": 25}
WITH_REFERENCE = 312
SETTLEMENT_DAYS = 210
#: The first line in the API's order (`settled_on`, then id), and a foreign one.
FIRST_ID = "proc:stl_c0593907109f"
USD_ID = "proc:stl_005f07b324a7"


def raw_lines(name: str) -> list[str]:
    """Every non-blank line of a fixture file, exactly as the generator wrote it."""
    text = (PROCESSOR_DIR / name).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def api_order(name: str, date_field: str) -> list[str]:
    """A fixture file's lines in the order the API serves them."""
    return sorted(
        raw_lines(name), key=lambda line: (json.loads(line)[date_field], json.loads(line)["id"])
    )


class FakeClock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[int] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: int) -> None:
        """What the client is given instead of `time.sleep`: time passes, nobody waits."""
        self.sleeps.append(seconds)
        self.now += seconds


class Seen(NamedTuple):
    url: str
    headers: dict[str, str]


class AppTransport:
    """The real mock API, in process."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.requests: list[Seen] = []

    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        self.requests.append(Seen(url, dict(headers)))
        response = self.client.get(url, headers=dict(headers), follow_redirects=False)
        return Reply(
            status=response.status_code,
            headers={name.lower(): value for name, value in response.headers.items()},
            body=response.content,
        )


class ScriptedTransport:
    """Answers in the order written. The last step repeats, so a storm is one line."""

    def __init__(self, *script: Reply | SourceDown) -> None:
        assert script
        self.script = list(script)
        self.requests: list[Seen] = []

    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        self.requests.append(Seen(url, dict(headers)))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, SourceDown):
            raise step
        return step


def reply(status: int, body: Any = b"", **headers: str) -> Reply:
    """A canned answer. A dict or list body is sent as JSON; header names use `_` for `-`."""
    if isinstance(body, dict | list):
        body = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    return Reply(
        status=status,
        headers={name.replace("_", "-").lower(): value for name, value in headers.items()},
        body=body,
    )


def api_error(status: int, code: str, message: str = "upstream prose", **headers: str) -> Reply:
    """An answer in the API's own error envelope."""
    return reply(status, {"error": {"code": code, "message": message}}, **headers)


def app_client(clock: FakeClock | None = None, **overrides: Any) -> TestClient:
    """A test client over a fresh app. Limits are generous unless a test asks otherwise."""
    values: dict[str, Any] = {
        "data_dir": DEV_SLICE,
        "token": TOKEN,
        "rate_per_second": 1000,
        "burst": 100_000,
    }
    values.update(overrides)
    settings = ApiSettings(**values)
    app = create_app(settings) if clock is None else create_app(settings, clock=clock)
    return TestClient(app, base_url=BASE_URL)


def client_settings(**overrides: Any) -> ProcessorSettings:
    """The server's settings for a test: the test token, and no database unless asked."""
    values: dict[str, Any] = {"base_url": BASE_URL, "token": TOKEN, "dsn": NO_DATABASE}
    values.update(overrides)
    return ProcessorSettings(**values)


@pytest.fixture(scope="session")
def api_client() -> Iterator[TestClient]:
    """One app for every test that does not care about the limiter."""
    with app_client() as client:
        yield client


@pytest.fixture
def transport(api_client: TestClient) -> AppTransport:
    return AppTransport(api_client)


# -- a real server on a real socket ----------------------------------------------------------


class Route(NamedTuple):
    status: int
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    #: Seconds to sit on the request before answering, for the timeout test.
    delay: float = 0.0


class Hit(NamedTuple):
    method: str
    path: str
    headers: dict[str, str]


class Stub:
    """A tiny HTTP server that answers any verb, and remembers which verb it was asked with."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.hits: list[Hit] = []
        self._release = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                path = self.path.split("?", 1)[0]
                stub.hits.append(
                    Hit(self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
                )
                route = stub.routes.get(path, Route(404, b'{"error":{"code":"not_found"}}'))
                if route.delay:
                    stub._release.wait(route.delay)
                self.send_response(route.status)
                for name, value in route.headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(route.body)))
                self.end_headers()
                self.wfile.write(route.body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _answer

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def stub() -> Iterator[Stub]:
    server = Stub()
    try:
        yield server
    finally:
        server.close()

"""The agent cannot reach a write through `mcp-processor` (plan §3.3, §4.4).

`mcp-erp` has a database role that cannot write and `mcp-bank` has one file handle opened
`"rb"`. An HTTP API offers neither, so the same boundary is drawn with what HTTP does have: the
verb. Five independent assertions, because a single one would be a promise rather than a
boundary:

1. the server advertises exactly the read-only allowlist, and says so in its annotations;
2. the package contains one request and its method is the literal `"GET"`, with no body -- and
   nothing else in it could send one, or touch a file;
3. what actually arrives at the API, and what actually goes over a socket, is a GET with no
   body, on every tool and on the failing paths as well as the good ones;
4. the source has nothing to write to in the first place: every other verb is refused by it;
5. the one thing it asks Postgres for goes through the same read-only access `mcp-erp` uses.
"""

import ast
import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from mcp import Client

import crossfoot.mcp.processor
from crossfoot.mcp.common.errors import READ_ONLY, ToolError
from crossfoot.mcp.common.parties import ErpParties, StaticParties
from crossfoot.mcp.erp.db import ReadOnlyDatabase
from crossfoot.mcp.processor import ProcessorService, ProcessorSettings, build_server
from crossfoot.mcp.processor.client import ProcessorApi
from crossfoot.mcp.processor.server import TOOL_NAMES
from crossfoot.mock_processor import ApiSettings, create_app
from tests.test_mcp.bank.test_no_write import MUTATORS
from tests.test_mcp.conftest import COUNTERPARTIES, DEV_SLICE
from tests.test_mcp.processor.conftest import (
    BASE_URL,
    FIRST_ID,
    SETTLEMENTS,
    TOKEN,
    AppTransport,
    FakeClock,
    Route,
    Stub,
    api_order,
    client_settings,
    raw_lines,
)

PACKAGE = Path(crossfoot.mcp.processor.__file__).parent
BATCH_ID = "bat_2024-01-11_01"

#: The verbs that change something. None of them may appear as a string anywhere in the package.
WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Anything by these names sends a request, whichever object it is called on. `Request` and the
#: opener's `open` are the two this package is allowed, once each, and are checked by hand.
SENDERS = frozenset(
    {"post", "put", "patch", "delete", "request", "urlopen", "urlretrieve", "send", "sendall"}
)
#: Modules with no read-only use here. `os` is allowed for `os.environ` and nothing else.
FORBIDDEN_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "subprocess",
        "shutil",
        "tempfile",
        "io",
        "fileinput",
        "requests",
        "httpx",
        "httpx2",
        "aiohttp",
        "ftplib",
        "smtplib",
    }
)


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _parsed(directory: Path) -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(directory.rglob("*.py")):
        yield path.name, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def modules() -> list[tuple[str, ast.Module]]:
    return list(_parsed(PACKAGE))


def calls(trees: list[tuple[str, ast.Module]], name: str) -> list[tuple[str, ast.Call]]:
    return [
        (module, node)
        for module, tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == name
    ]


def imports(trees: list[tuple[str, ast.Module]]) -> list[tuple[str, str, tuple[str, ...]]]:
    """Every import as (module file, imported module, names taken from it)."""
    found: list[tuple[str, str, tuple[str, ...]]] = []
    for module, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found += [(module, alias.name, ()) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.append((module, node.module, tuple(alias.name for alias in node.names)))
    return found


def request_problems(trees: list[tuple[str, ast.Module]]) -> list[str]:
    """Everything about how the package sends requests that is not "one GET, no body"."""
    problems: list[str] = []
    built = calls(trees, "Request")
    if [module for module, _ in built] != ["http.py"]:
        problems.append(f"Request is built in {[module for module, _ in built]}")
    for module, node in built:
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        method = keywords.get("method")
        if not (isinstance(method, ast.Constant) and method.value == "GET"):
            problems.append(f"{module}:{node.lineno} Request without the literal method GET")
        if len(node.args) != 1 or "data" in keywords or None in keywords:
            problems.append(f"{module}:{node.lineno} Request may carry a body")
    opened = calls(trees, "open")
    if [module for module, _ in opened] != ["http.py"]:
        problems.append(f"open is called in {[module for module, _ in opened]}")
    for module, node in opened:
        names = {keyword.arg for keyword in node.keywords}
        # `opener.open(request, timeout=...)`: a second positional argument would be a body,
        # and a bare `open(...)` would be a file.
        if isinstance(node.func, ast.Name) or len(node.args) != 1 or names != {"timeout"}:
            problems.append(f"{module}:{node.lineno} open is not opener.open(request, timeout=)")
    for module, tree in trees:
        for item in ast.walk(tree):
            if isinstance(item, ast.Call) and _called_name(item) in SENDERS:
                problems.append(f"{module}:{item.lineno} calls {_called_name(item)}")
            if isinstance(item, ast.keyword) and item.arg == "method":
                value = item.value
                if not (isinstance(value, ast.Constant) and value.value == "GET"):
                    problems.append(f"{module}:{value.lineno} a method that is not GET")
            if (
                isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value.strip().upper() in WRITE_VERBS
            ):
                problems.append(f"{module}:{item.lineno} names the verb {item.value!r}")
    return problems


# -- 1. what the server says it is -----------------------------------------------------------


def test_the_server_advertises_exactly_the_read_only_allowlist(
    offline: ProcessorService,
) -> None:
    server = build_server(offline.settings, offline)

    async def listed() -> list[tuple[str, bool | None, bool | None]]:
        async with Client(server) as client:
            result = await client.list_tools()
            return [
                (
                    tool.name,
                    None if tool.annotations is None else tool.annotations.read_only_hint,
                    None if tool.annotations is None else tool.annotations.destructive_hint,
                )
                for tool in result.tools
            ]

    tools = asyncio.run(listed())
    assert tuple(name for name, _, _ in tools) == TOOL_NAMES
    for name, read_only, destructive in tools:
        assert read_only is True, f"{name} is not annotated read-only"
        assert destructive is False, f"{name} is not annotated non-destructive"


# -- 2. what the code can do -----------------------------------------------------------------


def test_the_package_sends_one_request_and_it_is_a_get_with_no_body() -> None:
    assert request_problems(modules()) == []


def test_only_one_module_can_reach_the_network() -> None:
    found = imports(modules())
    network = [(module, name) for module, name, _ in found if name.startswith("urllib.request")]
    assert network == [("http.py", "urllib.request")]
    assert {module for module, name, _ in found if name == "urllib.error"} == {"http.py"}
    # `http.client` is imported for one exception class to catch, and for nothing that connects.
    assert [(module, names) for module, name, names in found if name.startswith("http")] == [
        ("http.py", ("HTTPException",))
    ]
    # The rest of `urllib` in the package builds and reads URLs; it does not open them.
    assert {name for _, name, _ in found if name.startswith("urllib")} == {
        "urllib.request",
        "urllib.error",
        "urllib.parse",
    }
    assert {name.split(".")[0] for _, name, _ in found}.isdisjoint(FORBIDDEN_MODULES)


def test_the_package_touches_no_file_and_uses_os_for_configuration_only() -> None:
    trees = modules()
    offenders = [
        f"{module}:{node.lineno} {_called_name(node)}"
        for module, tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) in MUTATORS
    ]
    assert offenders == []
    os_attributes = {
        node.attr
        for _, tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    }
    assert os_attributes == {"environ"}


def test_the_scanner_would_catch_a_write_if_there_were_one() -> None:
    # A guard that cannot fail proves nothing, so show it failing on each thing it guards.
    def problems(source: str, module: str = "http.py") -> list[str]:
        return request_problems([(module, ast.parse(source))])

    honest = 'opener.open(Request(url, headers=h, method="GET"), timeout=5)\n'
    assert problems(honest) == []
    planted = {
        "a second place a request is built": (honest, "client.py"),
        "another verb": ('opener.open(Request(url, method="POST"), timeout=5)\n', "http.py"),
        "a verb chosen at run time": (
            "opener.open(Request(url, method=verb), timeout=5)\n",
            "http.py",
        ),
        "no method, so urllib picks one": ("opener.open(Request(url), timeout=5)\n", "http.py"),
        "a body by keyword": (
            'opener.open(Request(url, data=b"x", method="GET"), timeout=5)\n',
            "http.py",
        ),
        "a body by position": (
            'opener.open(Request(url, b"x", method="GET"), timeout=5)\n',
            "http.py",
        ),
        "a body on the opener": (
            'opener.open(Request(url, method="GET"), b"x", timeout=5)\n',
            "http.py",
        ),
        "a file": (honest + 'open("settlements.jsonl", "w")\n', "http.py"),
        "a client library": (honest + "session.post(url, json=payload)\n", "http.py"),
        "urlopen": (honest + "urlopen(url)\n", "http.py"),
        "a verb in a table": (honest + 'VERBS = ("GET", "delete")\n', "http.py"),
    }
    for what, (source, module) in planted.items():
        assert problems(source, module) != [], what


# -- 3. what the code does -------------------------------------------------------------------


class Recorder:
    """ASGI middleware: remembers the verb and body size of everything that reaches the API."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.seen: list[tuple[str, str, int]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {name.decode().lower(): value.decode() for name, value in scope["headers"]}
            self.seen.append(
                (scope["method"], scope["path"], int(headers.get("content-length", "0")))
            )
        await self.app(scope, receive, send)


def exercise(server: Any) -> list[str]:
    """Every tool, on paths that succeed and paths that fail. Returns each answer's code."""
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("get_settlement", {"id": FIRST_ID}),
        ("get_settlement", {"id": "proc:stl_000000000000"}),
        ("get_settlement", {"id": "not an id"}),
        ("list_settlements", {"date_from": "2024-06-03", "date_to": "2024-06-03"}),
        ("list_settlements", {"date_from": "nope"}),
        ("list_settlements", {"limit": 10_000}),
        ("get_batch", {"id": "BAT_2024-07-17_01"}),
        ("get_batch", {"id": "bat_2030-01-01_01"}),
        ("export_settlements", {"limit": 200}),
        ("export_settlements", {"cursor": "forged"}),
        ("export_batches", {"limit": 5}),
    ]

    async def run() -> list[dict[str, Any]]:
        async with Client(server) as client:
            results = [await client.call_tool(name, arguments) for name, arguments in attempts]
            assert all(result.is_error is False for result in results)
            return [dict(result.structured_content or {}) for result in results]

    answers = asyncio.run(run())
    assert TOKEN not in json.dumps(answers)
    return [answer["error"]["code"] if "error" in answer else "ok" for answer in answers]


EXPECTED_CODES = [
    "ok",
    "not_found",
    "invalid_argument",
    "ok",
    "invalid_argument",
    "limit_exceeded",
    "ok",
    "not_found",
    "ok",
    "invalid_argument",
    "ok",
]


def test_everything_that_reaches_the_api_is_a_get_with_no_body(
    static_parties: StaticParties,
) -> None:
    recorder = Recorder(
        create_app(
            ApiSettings(data_dir=DEV_SLICE, token=TOKEN, rate_per_second=1000, burst=100_000)
        )
    )
    settings = client_settings()
    with TestClient(recorder, base_url=BASE_URL) as client:
        api = ProcessorApi(settings, AppTransport(client), FakeClock().sleep)
        service = ProcessorService(settings, parties=static_parties, api=api)
        assert exercise(build_server(settings, service)) == EXPECTED_CODES
    # The calls did real work, so "only reads" is not "nothing happened".
    assert len(recorder.seen) == 9
    assert {method for method, _, _ in recorder.seen} == {"GET"}
    assert {size for _, _, size in recorder.seen} == {0}


def served_by(stub: Stub) -> None:
    """Enough of the API on a real socket for every tool to do real work."""
    for line in raw_lines("settlements.jsonl"):
        record = json.loads(line)
        path = f"/v1/settlements/{record['id'].replace(':', '%3A')}"
        stub.routes[path] = Route(200, line.encode("utf-8"))
    for line in raw_lines("batches.jsonl"):
        stub.routes[f"/v1/batches/{json.loads(line)['id']}"] = Route(200, line.encode("utf-8"))

    def page(lines: list[str]) -> bytes:
        return ('{"data":[' + ",".join(lines) + '],"next_cursor":null,"has_more":false}').encode()

    stub.routes["/v1/settlements"] = Route(200, page(api_order("settlements.jsonl", "settled_on")))
    stub.routes["/v1/batches"] = Route(200, page(api_order("batches.jsonl", "closed_on")))


def test_everything_that_goes_over_a_socket_is_a_get_with_no_body(
    stub: Stub, static_parties: StaticParties
) -> None:
    served_by(stub)
    settings = client_settings(base_url=stub.url, max_limit=1000, default_limit=1000)
    service = ProcessorService(settings, parties=static_parties)
    assert service.get_settlement(FIRST_ID).record_id == FIRST_ID
    with pytest.raises(ToolError):
        service.get_settlement("proc:stl_000000000000")
    assert len(service.export_settlements().records) == SETTLEMENTS
    assert len(service.export_batches().batches) == 268
    assert service.get_batch("BAT_2024-07-17_01").total_lines == 8
    with pytest.raises(ToolError):
        # The stub ignores the date filter, so this window is answered with every line.
        service.list_settlements(date_from="2024-06-03", date_to="2024-06-03")

    assert len(stub.hits) == 7
    assert {hit.method for hit in stub.hits} == {"GET"}
    for hit in stub.hits:
        assert "content-length" not in hit.headers
        assert "transfer-encoding" not in hit.headers
        assert hit.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in hit.path


# -- 4. what the source would do with a write -------------------------------------------------


def test_the_api_has_no_endpoint_that_takes_anything_but_a_read(api_client: TestClient) -> None:
    app = create_app(ApiSettings(data_dir=DEV_SLICE, token=TOKEN))
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert sorted(route.path for route in routes if route.path.startswith("/v1")) == [
        "/v1/batches",
        "/v1/batches/{batch_id}",
        "/v1/settlements",
        "/v1/settlements/{settlement_id}",
    ]
    assert all(set(route.methods or ()) <= {"GET", "HEAD"} for route in routes)
    assert all(route.methods for route in routes)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    for path in ("/v1/settlements", f"/v1/settlements/{FIRST_ID}", f"/v1/batches/{BATCH_ID}"):
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            assert api_client.request(verb, path, headers=auth, json={}).status_code == 405


# -- 5. the other source ---------------------------------------------------------------------


def test_the_names_come_through_a_connection_that_refuses_every_write(
    processor_settings: ProcessorSettings,
) -> None:
    # What the service builds for itself when it is not handed a name list.
    assert isinstance(ProcessorService(processor_settings).parties, ErpParties)

    db = ReadOnlyDatabase(processor_settings.dsn, connect_timeout=5)
    parties = ErpParties(processor_settings.dsn, processor_settings.erp_schema, db=db)
    try:
        assert len(parties.name_index()) == COUNTERPARTIES
        for statement in (
            f"DELETE FROM {processor_settings.erp_schema}.counterparties",
            f"UPDATE {processor_settings.erp_schema}.counterparties SET legal_name = 'X'",
        ):
            with pytest.raises(ToolError) as caught, db.cursor() as cur:
                cur.execute(statement)
            assert caught.value.code == READ_ONLY
        assert parties.registry().frozen is True
    finally:
        parties.close()

"""The agent cannot reach a write through `mcp-bank` (plan §3.3, §4.4).

`mcp-erp` has a database role that cannot write. A directory has no such role, so the same
boundary is drawn with what a file source does have. Four independent assertions, because a
single one would be a promise rather than a boundary:

1. the server advertises exactly the read-only allowlist, and says so in its annotations;
2. the package contains no call that could create, change or remove a file -- its one `open`
   is in `files.py` and its mode is `"rb"`;
3. every tool, on every path including the failing ones, leaves the export byte for byte and
   timestamp for timestamp as it found it, and does so against a copy it is not permitted to
   modify;
4. the one thing it asks Postgres for goes through the same read-only access `mcp-erp` uses,
   which refuses a write even here.
"""

import ast
import asyncio
import hashlib
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

import crossfoot.mcp.bank
from crossfoot.mcp.bank import BankService, BankSettings, build_server
from crossfoot.mcp.bank.parties import ErpParties, StaticParties
from crossfoot.mcp.bank.server import TOOL_NAMES
from crossfoot.mcp.common.errors import READ_ONLY, ToolError
from crossfoot.mcp.erp.db import ReadOnlyDatabase
from tests.test_mcp.bank.conftest import BANK_DIR, LINES, NO_DATABASE
from tests.test_mcp.conftest import COUNTERPARTIES

PACKAGE = Path(crossfoot.mcp.bank.__file__).parent

#: Anything by these names creates, changes or removes a file, whichever object it is called
#: on. None of them may be called anywhere in the package.
MUTATORS = frozenset(
    {
        "write",
        "writelines",
        "write_text",
        "write_bytes",
        "unlink",
        "remove",
        "rmdir",
        "rmtree",
        "rename",
        "renames",
        "mkdir",
        "makedirs",
        "touch",
        "truncate",
        "chmod",
        "chown",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "move",
        "symlink_to",
        "hardlink_to",
        "link_to",
        "mkstemp",
        "mkdtemp",
        "NamedTemporaryFile",
        "TemporaryFile",
        "TemporaryDirectory",
    }
)
#: Modules with no read-only use here. `os` is allowed for `os.environ` and nothing else, which
#: is also what rules out `os.system`, `os.remove` and the rest without listing them.
FORBIDDEN_MODULES = frozenset({"shutil", "tempfile", "subprocess", "io", "fileinput"})
READ_MODES = frozenset({"r", "rb"})


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _open_mode(node: ast.Call) -> str | None:
    """The literal mode of an `open` call: second positional, or `mode=`. None if not literal."""
    builtin = isinstance(node.func, ast.Name)
    position = 1 if builtin else 0  # `open(path, mode)` vs `path.open(mode)`
    candidates = [node.args[position]] if len(node.args) > position else []
    candidates += [keyword.value for keyword in node.keywords if keyword.arg == "mode"]
    if not candidates:
        return "r"
    mode = candidates[0]
    return mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else None


def modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path.name, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# -- 1. what the server says it is -----------------------------------------------------------


def test_the_server_advertises_exactly_the_read_only_allowlist(offline: BankService) -> None:
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


def test_the_package_opens_one_file_handle_and_it_is_read_only() -> None:
    opens = [
        (name, _open_mode(node))
        for name, tree in modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == "open"
    ]
    # One handle, in the module whose job it is, in a mode that cannot write.
    assert opens == [("files.py", "rb")]
    assert all(mode in READ_MODES for _, mode in opens)


def test_the_package_calls_nothing_that_changes_a_file() -> None:
    offenders = [
        f"{name}:{node.lineno} {_called_name(node)}"
        for name, tree in modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) in MUTATORS
    ]
    assert offenders == []


def test_the_package_imports_no_module_that_exists_to_change_files() -> None:
    imported: set[str] = set()
    os_attributes: set[str] = set()
    for _, tree in modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                os_attributes.add(node.attr)
    assert imported.isdisjoint(FORBIDDEN_MODULES)
    # `os` is imported for configuration, and is used for nothing else.
    assert os_attributes == {"environ"}


def test_the_scanner_would_catch_a_write_if_there_were_one() -> None:
    # A guard that cannot fail proves nothing, so show it failing on the thing it guards.
    sneaky = ast.parse(
        "from pathlib import Path\n"
        "def f(p: Path) -> None:\n"
        "    with open(p, 'w') as h:\n"
        "        h.write('x')\n"
        "    with p.open('ab') as h:\n"
        "        pass\n"
        "    p.unlink()\n"
    )
    calls = [node for node in ast.walk(sneaky) if isinstance(node, ast.Call)]
    assert [_open_mode(node) for node in calls if _called_name(node) == "open"] == ["w", "ab"]
    assert sorted(name for node in calls if (name := _called_name(node)) in MUTATORS) == [
        "unlink",
        "write",
    ]


# -- 3. what the code does -------------------------------------------------------------------


def snapshot(directory: Path) -> dict[str, tuple[str, int, int]]:
    """Every file's content hash, size and modification time."""
    return {
        path.name: (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(directory.iterdir())
    }


@pytest.fixture
def locked_export(tmp_path: Path) -> Iterator[Path]:
    """A copy of the export with every file marked read-only, and one statement broken.

    The broken month is there so the failing paths run too: an error handler is as good a place
    as any for a stray write. Permissions are restored afterwards so pytest can clean up.
    """
    data = tmp_path / "data"
    bank = data / "bank"
    shutil.copytree(BANK_DIR, bank)
    december = bank / "2024-12.csv"
    december.write_bytes(december.read_bytes().replace(b",FPI,", b",CHQ,", 1))
    files = sorted(bank.iterdir())
    for path in files:
        path.chmod(stat.S_IREAD)
    try:
        yield data
    finally:
        for path in files:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)


def exercise(server: Any) -> list[dict[str, Any]]:
    """Every tool, on paths that succeed and paths that fail."""
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_bank_line", {"id": "bank:2024-03-01:000001"}),
        ("get_bank_line", {"id": "bank:2024-04-02:000003"}),
        ("get_bank_line", {"id": "bank:2024-03-01:000999"}),
        ("get_bank_line", {"id": "not an id"}),
        ("get_bank_line", {"id": "bank:2024-12-02:000001"}),
        ("find_bank_lines", {"date_from": "2024-06-03", "date_to": "2024-06-03"}),
        ("find_bank_lines", {"amount_max_minor": -1, "date_to": "2024-11-30"}),
        ("find_bank_lines", {"date_from": "2024-12-01"}),
        ("find_bank_lines", {"limit": 10_000}),
        ("export_bank_lines", {"limit": 200}),
        ("export_bank_lines", {"after_id": "bank:2024-11-29:000101", "limit": 5}),
    ]

    async def run() -> list[dict[str, Any]]:
        async with Client(server) as client:
            results = [await client.call_tool(name, arguments) for name, arguments in calls]
            assert all(result.is_error is False for result in results)
            return [dict(result.structured_content or {}) for result in results]

    return asyncio.run(run())


def test_every_tool_leaves_the_export_exactly_as_it_found_it(
    locked_export: Path, static_parties: StaticParties
) -> None:
    bank_dir = locked_export / "bank"
    with pytest.raises(PermissionError):
        (bank_dir / "2024-03.csv").write_bytes(b"the copy really is locked")

    before = snapshot(bank_dir)
    settings = BankSettings(data_dir=locked_export, dsn=NO_DATABASE)
    answers = exercise(build_server(settings, BankService(settings, parties=static_parties)))
    after = snapshot(bank_dir)

    # Same files, same bytes, same timestamps: nothing written, nothing created, nothing gone.
    assert after == before
    assert len(before) == 14
    # And the calls did real work, so "nothing changed" is not "nothing happened".
    codes = [answer["error"]["code"] if "error" in answer else "ok" for answer in answers]
    assert codes == [
        "ok",
        "ok",
        "not_found",
        "invalid_argument",
        "malformed_source",
        "ok",
        "ok",
        "malformed_source",
        "limit_exceeded",
        "ok",
        "malformed_source",
    ]
    assert answers[9]["count"] == 200


def test_reading_the_committed_slice_does_not_touch_it(offline: BankService) -> None:
    before = snapshot(BANK_DIR)
    seen = 0
    after_id: str | None = None
    while page := offline.export_bank_lines(after_id=after_id, limit=200):
        seen += len(page)
        after_id = page[-1].record_id
    assert seen == LINES
    assert snapshot(BANK_DIR) == before


# -- 4. the other source ---------------------------------------------------------------------


def test_the_names_come_through_a_connection_that_refuses_every_write(
    bank_settings: BankSettings,
) -> None:
    db = ReadOnlyDatabase(bank_settings.dsn, connect_timeout=5)
    parties = ErpParties(bank_settings.dsn, bank_settings.erp_schema, db=db)
    try:
        assert len(parties.name_index()) == COUNTERPARTIES
        for statement in (
            f"DELETE FROM {bank_settings.erp_schema}.counterparties",
            f"UPDATE {bank_settings.erp_schema}.counterparties SET legal_name = 'X'",
            "INSERT INTO er.counterparty_alias (alias_norm, alias_raw, counterparty_id, "
            "provenance, escalation_id, confirmed_by, confirmed_at) "
            "VALUES ('X', 'X', 1, 'human_confirmed', 'esc-1', 'nobody', now())",
        ):
            with pytest.raises(ToolError) as caught, db.cursor() as cur:
                cur.execute(statement)
            assert caught.value.code == READ_ONLY
        # Still usable afterwards, and still frozen.
        assert parties.registry().frozen is True
    finally:
        parties.close()

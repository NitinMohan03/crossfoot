"""The agent cannot reach a write through `mcp-remittance`, nor the server the answers.

Two boundaries are drawn here, each by more than one independent assertion.

**Read-only** (plan §3.3, §4.4), as for `mcp-bank`, since this source is files too:

1. the server advertises exactly the read-only allowlist, and says so in its annotations;
2. the package contains no call that could create, change or remove a file -- its one `open`
   is in `files.py` and its mode is `"rb"` -- and the PDF library is handed bytes in memory,
   never a path it could open for itself;
3. every tool, on every path including the failing ones, leaves the dataset byte for byte and
   timestamp for timestamp as it found it, against a copy it is not permitted to modify;
4. the one thing it asks Postgres for goes through the same read-only access `mcp-erp` uses.

**Blind to the answers** (spec 01 §6). `remit.jsonl` sits in the same directory as the PDFs and
says what every advice contains and which layout drew it. The parser is graded against that
file, so it must not be able to read it:

5. nothing in the package imports the generator or the held-out constant, or names the truth
   file or a layout id outside a docstring;
6. the server gives the same answers when that file is missing or is garbage.
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

import crossfoot.mcp.remittance
from crossfoot.mcp.common.errors import READ_ONLY, ToolError
from crossfoot.mcp.common.parties import ErpParties, StaticParties
from crossfoot.mcp.erp.db import ReadOnlyDatabase
from crossfoot.mcp.remittance import RemittanceService, RemittanceSettings, build_server
from crossfoot.mcp.remittance.server import TOOL_NAMES
from crossfoot.mcp.remittance.settings import FX_FILE, REMIT_SUBDIR
from tests.test_mcp.bank.test_no_write import MUTATORS, READ_MODES, _called_name, _open_mode
from tests.test_mcp.conftest import COUNTERPARTIES, DEV_SLICE
from tests.test_mcp.remittance.conftest import (
    ADVICES,
    NO_DATABASE,
    REMIT_DIR,
    SMALL,
    Builder,
    rendered,
)
from tests.test_mcp.remittance.test_failures import other_document

PACKAGE = Path(crossfoot.mcp.remittance.__file__).parent

#: Modules with no read-only use here. `io` is absent from this list, unlike the bank's: the
#: PDF library needs a stream, and the next test pins what `io` is used for.
FORBIDDEN_MODULES = frozenset({"shutil", "tempfile", "subprocess", "fileinput"})
#: Where the answers are. Neither may be imported by the package under test.
ANSWERS = ("crossfoot.generate", "crossfoot.config")


def modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path.name, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imports(tree: ast.Module) -> list[tuple[str, tuple[str, ...]]]:
    """Every import in a module as (module, names); a bare `import x` has no names."""
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, ()) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, tuple(alias.name for alias in node.names)))
    return found


def strings_outside_docstrings(tree: ast.Module) -> list[str]:
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


# -- 1. what the server says it is -----------------------------------------------------------


def test_the_server_advertises_exactly_the_read_only_allowlist(
    offline: RemittanceService,
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
    assert TOOL_NAMES == (
        "get_remittance_for_payment",
        "get_remittance_lines",
        "export_remittances",
    )
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
    # One handle, in the module whose job it is, in a mode that cannot write. `_called_name`
    # matches `anything.open(...)` too, so this also says the PDF library's own `open` -- the
    # one that takes a path -- is never called.
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
        imported.update(module.split(".")[0] for module, _ in imports(tree))
        os_attributes.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )
    assert imported.isdisjoint(FORBIDDEN_MODULES)
    # `os` is imported for configuration, and is used for nothing else.
    assert os_attributes == {"environ"}


def test_io_is_used_for_one_in_memory_stream_and_nothing_else() -> None:
    uses = [
        (name, module, names)
        for name, tree in modules()
        for module, names in imports(tree)
        if module.split(".")[0] == "io"
    ]
    # `BytesIO` is memory. `io.open`, `io.FileIO` and the rest are not imported at all.
    assert uses == [("layout.py", "io", ("BytesIO",))]


def test_the_pdf_library_is_used_in_one_module_and_is_handed_memory_not_a_path() -> None:
    users = sorted(
        name
        for name, tree in modules()
        if any(module.split(".")[0] in ("pdfplumber", "pdfminer") for module, _ in imports(tree))
    )
    assert users == ["layout.py"]

    tree = dict(modules())["layout.py"]
    assert imports(tree).count(("pdfplumber.pdf", ("PDF",))) == 1
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "PDF"
    ]
    assert len(constructions) == 1
    (construction,) = constructions
    # `PDF(BytesIO(raw))`: one argument, a stream built in memory, and no `path=`.
    assert construction.keywords == []
    (argument,) = construction.args
    assert isinstance(argument, ast.Call)
    assert isinstance(argument.func, ast.Name) and argument.func.id == "BytesIO"
    # And nothing reaches the library by attribute (`PDF.open(path)`, `pdfplumber.open(path)`).
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("PDF", "pdfplumber")
    ]


def test_the_scanner_would_catch_a_write_if_there_were_one() -> None:
    # A guard that cannot fail proves nothing, so show it failing on the thing it guards.
    sneaky = ast.parse(
        "import pdfplumber\n"
        "from pathlib import Path\n"
        "def f(p: Path) -> None:\n"
        "    with open(p, 'wb') as h:\n"
        "        h.write(b'x')\n"
        "    pdfplumber.open(p)\n"
        "    p.unlink()\n"
    )
    calls = [node for node in ast.walk(sneaky) if isinstance(node, ast.Call)]
    # Two calls named `open`: the built-in in a writing mode, and the library's, whose first
    # argument is a path and not a literal mode at all.
    modes = [_open_mode(node) for node in calls if _called_name(node) == "open"]
    assert len(modes) == 2
    assert set(modes) == {"wb", None}
    assert sorted(name for node in calls if (name := _called_name(node)) in MUTATORS) == [
        "unlink",
        "write",
    ]


# -- 3. what the code does -------------------------------------------------------------------


def snapshot(data: Path) -> dict[str, tuple[str, int, int]]:
    """Every file's content hash, size and modification time, the rate table included."""
    files = [*sorted((data / REMIT_SUBDIR).iterdir()), data / FX_FILE]
    return {
        path.name: (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in files
    }


@pytest.fixture
def locked_dataset(tmp_path: Path) -> Iterator[Path]:
    """A small dataset with every file marked read-only, and three advices that will not read.

    The broken ones are there so the failing paths run too: an error handler is as good a place
    as any for a stray write. Permissions are restored afterwards so pytest can clean up.
    """
    data = tmp_path / "data"
    remit = data / REMIT_SUBDIR
    remit.mkdir(parents=True)
    for payment_ref in SMALL:
        shutil.copyfile(REMIT_DIR / f"{payment_ref}.pdf", remit / f"{payment_ref}.pdf")
    shutil.copyfile(DEV_SLICE / FX_FILE, data / FX_FILE)
    (remit / "PAY-2024-0002.pdf").write_bytes(b"not a pdf at all")
    (remit / "PAY-2024-0003.pdf").write_bytes(other_document())
    (remit / "PAY-2024-0500.pdf").write_bytes(
        rendered("B", payment_ref="PAY-2024-0500", total_minor=1)
    )
    (remit / "remit.jsonl").write_bytes(b'{"id":"remit:RA-2024-0001","template":"A"}\n')
    files = [*sorted(remit.iterdir()), data / FX_FILE]
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
        ("get_remittance_for_payment", {"payment_ref": "PAY-2024-0001"}),
        ("get_remittance_for_payment", {"payment_ref": "PAY-2024-0027"}),
        ("get_remittance_for_payment", {"payment_ref": "PAY-2024-0002"}),
        ("get_remittance_for_payment", {"payment_ref": "PAY-2024-0003"}),
        ("get_remittance_for_payment", {"payment_ref": "PAY-2024-0500"}),
        ("get_remittance_for_payment", {"payment_ref": "PAY-2024-9999"}),
        ("get_remittance_for_payment", {"payment_ref": "../fx_rates"}),
        ("get_remittance_lines", {"remittance_id": "remit:RA-2024-0004"}),
        ("get_remittance_lines", {"remittance_id": "remit:RA-2024-0003"}),
        ("get_remittance_lines", {"remittance_id": "not an id"}),
        ("export_remittances", {}),
        ("export_remittances", {"after_id": "remit:RA-2024-0003", "limit": 2}),
        ("export_remittances", {"limit": 10_000}),
    ]

    async def run() -> list[dict[str, Any]]:
        async with Client(server) as client:
            results = [await client.call_tool(name, arguments) for name, arguments in calls]
            assert all(result.is_error is False for result in results)
            return [dict(result.structured_content or {}) for result in results]

    return asyncio.run(run())


def test_every_tool_leaves_the_dataset_exactly_as_it_found_it(
    locked_dataset: Path, static_parties: StaticParties
) -> None:
    with pytest.raises(PermissionError):
        (locked_dataset / REMIT_SUBDIR / "PAY-2024-0001.pdf").write_bytes(b"really locked")
    with pytest.raises(PermissionError):
        (locked_dataset / FX_FILE).write_bytes(b"really locked")

    before = snapshot(locked_dataset)
    settings = RemittanceSettings(data_dir=locked_dataset, dsn=NO_DATABASE)
    answers = exercise(build_server(settings, RemittanceService(settings, parties=static_parties)))
    after = snapshot(locked_dataset)

    # Same files, same bytes, same timestamps: nothing written, nothing created, nothing gone.
    assert after == before
    assert len(before) == 8
    # And the calls did real work, so "nothing changed" is not "nothing happened".
    codes = [answer["error"]["code"] if "error" in answer else "ok" for answer in answers]
    assert codes == [
        "ok",
        "ok",
        "malformed_source",
        "malformed_source",
        "malformed_source",
        "not_found",
        "invalid_argument",
        "ok",
        "malformed_source",
        "invalid_argument",
        "ok",
        "ok",
        "limit_exceeded",
    ]
    assert (answers[10]["count"], len(answers[10]["unparsed"])) == (3, 3)
    assert (answers[11]["count"], len(answers[11]["unparsed"])) == (2, 0)


def test_reading_the_committed_slice_does_not_touch_it(static_parties: StaticParties) -> None:
    # The committed directory itself, not a copy. Only developed advices are asked for.
    before = snapshot(DEV_SLICE)
    assert len(before) == ADVICES + 2  # the PDFs, `remit.jsonl`, and the rate table
    service = RemittanceService(
        RemittanceSettings(data_dir=DEV_SLICE, dsn=NO_DATABASE), parties=static_parties
    )
    for payment_ref in SMALL:
        assert service.get_remittance_for_payment(payment_ref).reference == payment_ref
    assert snapshot(DEV_SLICE) == before


# -- 4. the other source ---------------------------------------------------------------------


def test_the_names_come_through_a_connection_that_refuses_every_write(
    remittance_settings: RemittanceSettings,
) -> None:
    db = ReadOnlyDatabase(remittance_settings.dsn, connect_timeout=5)
    parties = ErpParties(remittance_settings.dsn, remittance_settings.erp_schema, db=db)
    try:
        assert len(parties.name_index()) == COUNTERPARTIES
        for statement in (
            f"DELETE FROM {remittance_settings.erp_schema}.counterparties",
            f"UPDATE {remittance_settings.erp_schema}.counterparties SET legal_name = 'X'",
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


# -- 5 and 6. blind to the answers -----------------------------------------------------------


def test_the_package_imports_neither_the_generator_nor_the_held_out_constant() -> None:
    offenders = [
        f"{name}: {module}"
        for name, tree in modules()
        for module, _ in imports(tree)
        if any(module == answer or module.startswith(f"{answer}.") for answer in ANSWERS)
    ]
    assert offenders == []
    names = {
        node.id for _, tree in modules() for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert "HELD_OUT_TEMPLATE" not in names


def test_no_code_names_the_truth_file_or_a_layout_id() -> None:
    # Docstrings explain why the file is not read; no executable string may name it.
    offenders = [
        f"{name}: {text!r}"
        for name, tree in modules()
        for text in strings_outside_docstrings(tree)
        if "remit.jsonl" in text or ".jsonl" in text or "template" in text.lower()
    ]
    assert offenders == []


def test_the_scanner_would_catch_a_peek_at_the_answers() -> None:
    sneaky = ast.parse(
        '"""Never reads remit.jsonl."""\n'
        "from crossfoot.config import HELD_OUT_TEMPLATE\n"
        "from crossfoot.generate.read import read_remit\n"
        "TRUTH = 'remit.jsonl'\n"
    )
    assert [module for module, _ in imports(sneaky)] == [
        "crossfoot.config",
        "crossfoot.generate.read",
    ]
    assert strings_outside_docstrings(sneaky) == ["remit.jsonl"]


def test_the_answers_are_the_same_with_the_truth_file_missing_or_garbage(
    broken: Builder, offline: RemittanceService
) -> None:
    # `offline` reads a directory with no `remit.jsonl` in it at all. This one has a file by
    # that name that says every advice is something else entirely.
    lying = broken(**{"remit.jsonl": b'{"id":"remit:RA-2024-0001","total_minor":1}\n\x00\xff'})
    page = lying.export_remittances()
    assert page.unparsed == ()
    assert [record.reference for record in page.records] == list(SMALL)
    for record in page.records:
        assert record == offline.get_remittance_for_payment(record.reference or "")

"""The page-size check, once, and the four services that all use it.

`limit()` was written in `mcp-erp` and copied twice. The fourth server made it a rule: one
function in `mcp/common/paging.py`, and no service keeps a copy of its own.
"""

import ast
from pathlib import Path

import pytest

import crossfoot.mcp
from crossfoot.mcp.common import page_limit
from crossfoot.mcp.common.errors import INVALID_ARGUMENT, LIMIT_EXCEEDED, ToolError

MCP = Path(crossfoot.mcp.__file__).parent
SERVICES = ("erp", "bank", "processor", "remittance")


def test_no_limit_is_the_default() -> None:
    assert page_limit(None, default=50, maximum=200) == 50


@pytest.mark.parametrize("asked", [1, 50, 200])
def test_a_limit_inside_the_bounds_is_itself(asked: int) -> None:
    assert page_limit(asked, default=50, maximum=200) == asked


@pytest.mark.parametrize("asked", [0, -1, -200])
def test_a_limit_below_one_is_the_callers_mistake(asked: int) -> None:
    with pytest.raises(ToolError) as caught:
        page_limit(asked, default=50, maximum=200)
    assert caught.value.code == INVALID_ARGUMENT
    assert str(asked) in caught.value.message


def test_a_limit_above_the_maximum_says_what_the_maximum_is() -> None:
    with pytest.raises(ToolError) as caught:
        page_limit(201, default=50, maximum=200)
    assert caught.value.code == LIMIT_EXCEEDED
    # The caller can ask again without guessing.
    assert caught.value.extra == {"max_limit": 200}
    assert caught.value.payload()["error"]["max_limit"] == 200


def test_every_service_uses_the_shared_check_and_keeps_no_copy() -> None:
    for name in SERVICES:
        path = MCP / name / "tools.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "page_limit"
        ]
        assert calls, f"{name}/tools.py does not call page_limit"
        # A copy would have to say "limit must be at least 1" itself.
        constants = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert not any("limit must be at least" in text for text in constants), name

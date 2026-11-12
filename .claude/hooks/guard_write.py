"""PreToolUse guard for Write/Edit. Stdlib only so it runs under any Python.

Denies:
  1. any write under tests/fixtures/golden/  (ground truth)
  2. `float(` under src/crossfoot/{matching,blocking,models,generate}/  (money is Decimal)
  3. `import anthropic` / `from anthropic` outside src/crossfoot/llm/
     (all API calls go through llm/)
"""

import json
import os
import re
import sys

GOLDEN = "tests/fixtures/golden/"
NO_FLOAT = (
    "src/crossfoot/matching/",
    "src/crossfoot/blocking/",
    "src/crossfoot/models/",
    "src/crossfoot/generate/",
    "src/crossfoot/db/",
    "src/crossfoot/entities/",
    "src/crossfoot/mcp/",
    "src/crossfoot/ingest/",
)
LLM_ONLY = "src/crossfoot/llm/"
ANTHROPIC_IMPORT = re.compile(r"^\s*(import\s+anthropic\b|from\s+anthropic\b)", re.MULTILINE)


def deny(reason: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out))
    sys.exit(0)


def main() -> None:
    payload = json.load(sys.stdin)
    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path")
    if not file_path:
        return

    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    rel = os.path.relpath(os.path.abspath(file_path), root).replace(os.sep, "/")
    content = tool_input.get("content") or tool_input.get("new_string") or ""

    if rel.startswith(GOLDEN):
        deny(f"{rel}: tests/fixtures/golden/ is ground truth and must never be edited.")

    if rel.startswith(NO_FLOAT) and "float(" in content:
        deny(f"{rel}: `float(` is banned here. Money is Decimal or integer minor units.")

    outside_llm = rel.startswith("src/crossfoot/") and not rel.startswith(LLM_ONLY)
    if outside_llm and ANTHROPIC_IMPORT.search(content):
        deny(f"{rel}: anthropic may only be imported under src/crossfoot/llm/.")


if __name__ == "__main__":
    main()

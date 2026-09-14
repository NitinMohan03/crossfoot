"""PostToolUse: ruff (fix + format) then mypy on any edited .py file.

Runs the project venv's ruff/mypy so it works even when uv is not on PATH.
Exit 2 with the tool output on stderr so Claude sees failures.
"""

import json
import os
import subprocess
import sys


def venv_python(root: str) -> str:
    for candidate in (
        os.path.join(root, ".venv", "Scripts", "python.exe"),
        os.path.join(root, ".venv", "bin", "python"),
    ):
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def main() -> None:
    payload = json.load(sys.stdin)
    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or payload.get("tool_response", {}).get("filePath")
    if not file_path or not file_path.endswith(".py") or not os.path.exists(file_path):
        return

    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    py = venv_python(root)
    failed = False
    for cmd in (
        [py, "-m", "ruff", "check", "--fix", file_path],
        [py, "-m", "ruff", "format", file_path],
        [py, "-m", "mypy", file_path],
    ):
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        if result.returncode != 0:
            failed = True
            sys.stderr.write(f"$ {' '.join(cmd[1:])}\n{result.stdout}{result.stderr}\n")

    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()

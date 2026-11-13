"""The bank server's read-only access to the export. Rule 6, for a source that is files.

`mcp-erp` has a database role that cannot write. A directory has no such role, so the boundary
here is structural instead: **this module holds the only `open` in the package, and its mode is
`"rb"`.** Nothing else in `crossfoot.mcp.bank` touches the filesystem, and
`tests/test_mcp/bank/test_no_write.py` checks all three halves of that -- no other call that
could create, change or remove a file exists in the package, every tool leaves the export's
bytes and timestamps exactly as it found them, and the server runs unchanged against a copy it
has no permission to modify.

Bytes are decoded as strict UTF-8. A byte-order mark is tolerated because a spreadsheet re-save
adds one and it changes nothing; any other byte that is not UTF-8 is `malformed_source`, because
the alternative is a payer name quietly turned into mojibake and resolved against the wrong
counterparty. A directory that is not there is `unavailable`.
"""

import re
from pathlib import Path
from typing import Final

from crossfoot.mcp.common.errors import MALFORMED_SOURCE, UNAVAILABLE, ToolError

#: One file per month, `2024-03.csv` (spec 01 §3.2). Anything else in the directory is ignored.
MONTH_FILE: Final = re.compile(r"^(\d{4}-(?:0[1-9]|1[0-2]))\.csv$")
ENCODING: Final = "utf-8-sig"


class ReadOnlyExport:
    """The export directory, readable and nothing else."""

    def __init__(self, directory: Path, sidecar_name: str) -> None:
        self._directory = directory
        self._sidecar_name = sidecar_name

    @property
    def directory(self) -> Path:
        return self._directory

    def months(self) -> tuple[str, ...]:
        """Every month the export has a file for, as `YYYY-MM`, in order."""
        try:
            names = [entry.name for entry in self._directory.iterdir() if entry.is_file()]
        except OSError as exc:
            raise ToolError(
                UNAVAILABLE,
                f"no bank export at {self._directory.as_posix()} ({type(exc).__name__}); "
                "generate one with `just seed 42` or point --data at a dataset",
            ) from exc
        found = sorted(match.group(1) for name in names if (match := MONTH_FILE.match(name)))
        if not found:
            raise ToolError(
                UNAVAILABLE,
                f"{self._directory.as_posix()} holds no monthly statement (YYYY-MM.csv)",
            )
        return tuple(found)

    def month_text(self, month: str) -> str | None:
        """One month's CSV as text, or None when the export has no file for that month."""
        return self._text(f"{month}.csv")

    def sidecar_text(self) -> str | None:
        """The `posted_at` sidecar as text, or None when the export came without one."""
        return self._text(self._sidecar_name)

    def _text(self, name: str) -> str | None:
        raw = self._read(self._directory / name)
        if raw is None:
            return None
        try:
            return raw.decode(ENCODING)
        except UnicodeDecodeError as exc:
            raise ToolError(
                MALFORMED_SOURCE,
                f"{name} is not UTF-8: byte 0x{raw[exc.start]:02x} at offset {exc.start}",
                file=name,
            ) from exc

    @staticmethod
    def _read(path: Path) -> bytes | None:
        """The package's one file handle: binary, read-only, closed before returning."""
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError as exc:
            # One missing month is an answer ("no such line"); a missing export is not.
            if not path.parent.is_dir():
                raise ToolError(
                    UNAVAILABLE,
                    f"no bank export at {path.parent.as_posix()}; "
                    "generate one with `just seed 42` or point --data at a dataset",
                ) from exc
            return None
        except OSError as exc:
            raise ToolError(
                UNAVAILABLE, f"{path.name} could not be read ({type(exc).__name__})", file=path.name
            ) from exc

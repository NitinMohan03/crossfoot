"""The remittance server's read-only access to the advices. Rule 6, for a source that is files.

As in `mcp-bank`, a directory has no role that cannot write, so the boundary is structural:
**this module holds the only `open` in the package, and its mode is `"rb"`.** Bytes leave here
and nothing else in `crossfoot.mcp.remittance` touches the filesystem -- the PDF library is
handed those bytes in memory and is never given a path. `tests/test_mcp/remittance/
test_no_write.py` checks it on the syntax tree and against a copy the server may not modify.

**A file name is never built from caller text.** An advice is `<payment_ref>.pdf`, and the only
strings that reach a path are ones that already matched `PAYMENT_REF` -- digits and the literal
`PAY-`. `../erp.sql` is not a payment reference, so it is not a path either.

**What this module does not read is as deliberate as what it does.** The directory also holds
`remit.jsonl`, the generator's structured truth for every advice, including which layout each
one uses. That file is what the parser is graded against (spec 01 §3.4, §6). The listing below
matches advice PDFs by name and nothing else, so the truth is never opened, and the server gives
the same answers when it is not there at all.
"""

import re
from pathlib import Path
from typing import Final

from crossfoot.mcp.common.errors import MALFORMED_SOURCE, UNAVAILABLE, ToolError

#: `PAY-2024-0088`: the payment reference printed on an advice, and its file's stem.
PAYMENT_REF: Final = re.compile(r"^PAY-(\d{4})-(\d{4,})$")
SUFFIX: Final = ".pdf"
ENCODING: Final = "utf-8-sig"
CHUNK: Final = 65_536


def ref_key(payment_ref: str) -> tuple[int, int]:
    """Year and number, as integers: the order advices are listed and paged in."""
    match = PAYMENT_REF.match(payment_ref)
    if match is None:
        raise ValueError(f"{payment_ref!r} is not a payment reference")
    return int(match.group(1)), int(match.group(2))


class ReadOnlyAdvices:
    """The advice directory and the dataset's rate table, readable and nothing else."""

    def __init__(self, directory: Path, fx_path: Path, max_bytes: int) -> None:
        self._directory = directory
        self._fx_path = fx_path
        self._max_bytes = max_bytes

    @property
    def directory(self) -> Path:
        return self._directory

    def payment_refs(self) -> tuple[str, ...]:
        """The payment reference of every advice on disk, in order. Other files are ignored."""
        try:
            names = [entry.name for entry in self._directory.iterdir() if entry.is_file()]
        except OSError as exc:
            raise self._no_directory(exc) from exc
        refs = [
            name.removesuffix(SUFFIX)
            for name in names
            if name.endswith(SUFFIX) and PAYMENT_REF.match(name.removesuffix(SUFFIX))
        ]
        return tuple(sorted(refs, key=ref_key))

    def pdf_bytes(self, payment_ref: str) -> bytes | None:
        """One advice's bytes, or None when no advice was sent for that payment."""
        ref_key(payment_ref)  # refuses anything that is not a reference before a path exists
        return self._capped(self._directory / f"{payment_ref}{SUFFIX}")

    def fx_text(self) -> str | None:
        """The dataset's `fx_rates.csv` as text, or None when the dataset has none."""
        raw = self._capped(self._fx_path)
        if raw is None:
            return None
        try:
            return raw.decode(ENCODING)
        except UnicodeDecodeError as exc:
            raise ToolError(
                MALFORMED_SOURCE,
                f"{self._fx_path.name} is not UTF-8: byte 0x{raw[exc.start]:02x} "
                f"at offset {exc.start}",
                file=self._fx_path.name,
            ) from exc

    def _capped(self, path: Path) -> bytes | None:
        """A file's bytes, refused when there are more of them than the cap allows."""
        raw = self._read(path)
        if raw is not None and len(raw) > self._max_bytes:
            raise ToolError(
                MALFORMED_SOURCE,
                f"{path.name} is larger than {self._max_bytes} bytes, which nothing this "
                "server reads is",
                file=path.name,
            )
        return raw

    def _no_directory(self, exc: OSError) -> ToolError:
        return ToolError(
            UNAVAILABLE,
            f"no remittance advices at {self._directory.as_posix()} ({type(exc).__name__}); "
            "generate a dataset with `just seed 42` or point --data at one",
        )

    def _read(self, path: Path) -> bytes | None:
        """The package's one file handle: binary, read-only, closed before returning.

        Read in small pieces up to one byte past the cap, so a file that is too large is known
        to be without being read to its end -- and so reading a 3 kB advice does not first ask
        the allocator for a buffer the size of the cap.
        """
        try:
            with open(path, "rb") as handle:
                pieces: list[bytes] = []
                wanted = self._max_bytes + 1
                while wanted > 0 and (piece := handle.read(min(CHUNK, wanted))):
                    pieces.append(piece)
                    wanted -= len(piece)
                return b"".join(pieces)
        except FileNotFoundError as exc:
            # One missing advice is an answer ("none was sent"); a missing dataset is not.
            if not path.parent.is_dir():
                raise self._no_directory(exc) from exc
            return None
        except OSError as exc:
            raise ToolError(
                UNAVAILABLE, f"{path.name} could not be read ({type(exc).__name__})", file=path.name
            ) from exc

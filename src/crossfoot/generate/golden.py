"""The golden gate: regenerating `tests/fixtures/golden/dev/` (spec 01 §8.3).

`tests/fixtures/golden/` is ground truth. Three things guard it, and they guard against
different mistakes:

  * the Write/Edit hook in `.claude/hooks/guard_write.py` stops a tool editing a file there;
  * `guards.require_golden_opt_in` stops the generator writing there without the flag;
  * this module stops a regeneration becoming an *unreviewed* one.

The last is the point of §8.3: "a regeneration is always a deliberate, reviewed commit". So
after regenerating and validating, this prints `git diff --stat` and **exits non-zero when the
golden tree changed and no commit message was supplied**. Passing a message is the reviewer
saying what changed and why, and it is committed with exactly that message.

The dev seed comes from `seeds.py`; it is never written into the justfile. `TEST_SEED` has no
path into this module at all -- the test slice is held out until Phase 8 (§8.2).
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

from crossfoot.generate import validate
from crossfoot.generate.dataset import SLICE_EVENTS, generate, write
from crossfoot.generate.guards import GOLDEN, GuardError, require_golden_opt_in
from crossfoot.generate.seeds import DEV_SEED

DEV_DIR: Final = GOLDEN / "dev"
MANIFEST: Final = DEV_DIR / "manifest.jsonl"
SHA_ENV: Final = "CROSSFOOT_GENERATOR_SHA"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=120)


def tree_is_dirty(path: Path) -> bool:
    """True when `path` holds changes git has not been told about yet."""
    result = git("status", "--porcelain", "--", str(path))
    return bool(result.stdout.strip())


def head_sha() -> str:
    result = git("rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def committed_sha() -> str | None:
    """The `generator_sha` recorded in the golden manifest as committed, if there is one.

    The manifest header carries the commit the dataset was generated from (spec 01 §5.2), and
    for a *committed* fixture that field is a trap: committing the slice moves HEAD, so the
    next regeneration would differ from it by that one value and the gate below would report a
    change on every run, for ever, never reaching "unchanged".

    So a regeneration is produced with the sha already on record, which makes a no-op
    regeneration byte-identical. Only when the content genuinely moves is the sha rewritten to
    the current HEAD -- giving it the meaning that is actually useful: *the commit at which this
    fixture's content last changed*, not the commit that happens to be checked out now.
    """
    result = git("show", f"HEAD:{MANIFEST.as_posix()}")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    header = json.loads(result.stdout.splitlines()[0])
    sha = header.get("generator_sha")
    return str(sha) if sha else None


def regenerate(sha: str) -> tuple[int, str]:
    """Write the slice with `sha` as its recorded provenance. Returns (events, type summary)."""
    os.environ[SHA_ENV] = sha
    dataset = generate(DEV_SEED, "slice", SLICE_EVENTS)
    write(DEV_DIR, dataset)
    counts = ", ".join(f"{name}={value}" for name, value in sorted(dataset.type_counts.items()))
    return len(dataset.events), counts


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    message = " ".join(args).strip()

    try:
        require_golden_opt_in(DEV_DIR)
    except GuardError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    pinned = committed_sha()
    events, counts = regenerate(pinned or head_sha())
    print(f"regenerated {DEV_DIR}: seed={DEV_SEED} events={events}")
    print(f"  types: {counts}\n")

    results = validate.run(DEV_DIR)
    failed = [result for result in results if not result.ok]
    for result in failed:
        print(f"{result.name}: FAIL")
        for failure in result.failures[:10]:
            print(f"    ! {failure}")
    if failed:
        print(f"\nrefusing to keep an invalid golden slice: {len(failed)} check(s) failed")
        return 1
    print(f"validator: {len(results)}/{len(results)} checks ok\n")

    diff = git("diff", "--stat", "--", str(GOLDEN))
    print(diff.stdout.strip() or "(no tracked changes)")

    if not tree_is_dirty(GOLDEN):
        print("\ngolden tree unchanged.")
        return 0

    if not message:
        print(
            "\nthe golden tree changed and no commit message was supplied.\n"
            "A regeneration is ground truth moving, so it is always a reviewed commit:\n"
            '  just golden "why the golden slice changed"',
            file=sys.stderr,
        )
        return 1

    # The content genuinely moved, so the provenance should name this regeneration rather than
    # whatever the previous one recorded.
    if pinned and pinned != head_sha():
        regenerate(head_sha())
        print("refreshed generator_sha to HEAD")

    add = git("add", "--", str(GOLDEN))
    if add.returncode != 0:
        print(f"error: git add failed: {add.stderr.strip()}", file=sys.stderr)
        return 1
    commit = git("commit", "-m", message, "--", str(GOLDEN))
    if commit.returncode != 0:
        print(f"error: git commit failed: {commit.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"\ncommitted the golden slice: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

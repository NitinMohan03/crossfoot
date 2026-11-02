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

import subprocess
import sys
from pathlib import Path
from typing import Final

from crossfoot.generate import validate
from crossfoot.generate.dataset import SLICE_EVENTS, generate, write
from crossfoot.generate.guards import GOLDEN, GuardError, require_golden_opt_in
from crossfoot.generate.seeds import DEV_SEED

DEV_DIR: Final = GOLDEN / "dev"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=120)


def tree_is_dirty(path: Path) -> bool:
    """True when `path` holds changes git has not been told about yet."""
    result = git("status", "--porcelain", "--", str(path))
    return bool(result.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    message = " ".join(args).strip()

    try:
        require_golden_opt_in(DEV_DIR)
    except GuardError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    dataset = generate(DEV_SEED, "slice", SLICE_EVENTS)
    write(DEV_DIR, dataset)
    counts = ", ".join(f"{name}={value}" for name, value in sorted(dataset.type_counts.items()))
    print(f"regenerated {DEV_DIR}: seed={DEV_SEED} events={len(dataset.events)}")
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

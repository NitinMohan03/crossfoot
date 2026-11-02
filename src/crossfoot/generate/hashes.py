"""Tree hashing, so `just seed-check` can prove a second run is byte-identical (spec 01 §9).

`python -m crossfoot.generate.hashes A B` compares two dataset directories and names the first
file that differs. With one path it prints the tree hash.
"""

import sys
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

CHUNK = 1 << 20


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def walk(root: Path) -> Iterator[tuple[str, Path]]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path.relative_to(root).as_posix(), path


def tree_hashes(root: Path) -> dict[str, str]:
    return {name: file_hash(path) for name, path in walk(root)}


def tree_hash(root: Path) -> str:
    digest = sha256()
    for name, value in sorted(tree_hashes(root).items()):
        digest.update(f"{name}:{value}\n".encode())
    return digest.hexdigest()


def compare(left: Path, right: Path) -> list[str]:
    """Differences between two dataset directories, as human-readable lines."""
    left_hashes, right_hashes = tree_hashes(left), tree_hashes(right)
    problems: list[str] = []
    for name in sorted(set(left_hashes) - set(right_hashes)):
        problems.append(f"only in {left.name}: {name}")
    for name in sorted(set(right_hashes) - set(left_hashes)):
        problems.append(f"only in {right.name}: {name}")
    for name in sorted(set(left_hashes) & set(right_hashes)):
        if left_hashes[name] != right_hashes[name]:
            problems.append(
                f"differs: {name} ({left_hashes[name][:12]} vs {right_hashes[name][:12]})"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m crossfoot.generate.hashes DIR [OTHER_DIR]", file=sys.stderr)
        return 2
    left = Path(args[0])
    if len(args) == 1:
        print(f"{tree_hash(left)}  {left}")
        return 0

    right = Path(args[1])
    problems = compare(left, right)
    if problems:
        print(f"NOT byte-identical: {len(problems)} difference(s)")
        for line in problems[:20]:
            print(f"    ! {line}")
        return 1
    count = len(tree_hashes(left))
    print(f"byte-identical: {count} files, tree sha256 {tree_hash(left)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

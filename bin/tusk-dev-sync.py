#!/usr/bin/env python3
"""tusk dev-sync — refresh .claude/bin/ from source bin/ (source-repo only).

Mirrors install.sh's "section 1" file copy without re-running the full
installer: copies bin/tusk, bin/tusk-*.py, and the canonical
UNDERSCORE_BIN_FILES into .claude/bin/, then refreshes the .hash sidecar
for tusk-lint.py.

Source-repo only — refuses to run when REPO_ROOT/bin/ does not exist (a
consumer install has nothing to sync from). dist-excluded scripts ARE
copied: they are still meaningful in source-repo dev (e.g. tusk lint
rule 18 reads tusk-generate-manifest.py).

Usage: tusk dev-sync [--dry-run]

Exit codes:
  0  Success
  2  Source bin/ or target .claude/bin/ missing
"""

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from tusk_underscore_bin_files import UNDERSCORE_BIN_FILES  # noqa: E402


def _refresh_lint_hash(target_bin: Path):
    lint_py = target_bin / "tusk-lint.py"
    if not lint_py.is_file():
        return None
    digest = hashlib.md5(lint_py.read_bytes()).hexdigest()
    hash_path = target_bin / "tusk-lint.py.hash"
    hash_path.write_text(digest + "\n")
    return hash_path


def main(argv):
    if not argv:
        print("Usage: tusk-dev-sync.py <repo_root> [--dry-run]", file=sys.stderr)
        return 2
    repo_root = Path(argv[0])
    rest = argv[1:]

    ap = argparse.ArgumentParser(allow_abbrev=False, prog="tusk dev-sync")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="List the files that would be copied without writing them.",
    )
    args = ap.parse_args(rest)

    source_bin = repo_root / "bin"
    target_bin = repo_root / ".claude" / "bin"

    if not source_bin.is_dir():
        print(
            f"Error: {source_bin} does not exist — tusk dev-sync only runs in the tusk source repo.",
            file=sys.stderr,
        )
        return 2

    if not target_bin.is_dir():
        print(
            f"Error: {target_bin} does not exist — tusk dev-sync requires a Claude Code install layout (.claude/bin/).",
            file=sys.stderr,
        )
        return 2

    copied = []

    src_tusk = source_bin / "tusk"
    if src_tusk.is_file():
        dst = target_bin / "tusk"
        if not args.dry_run:
            shutil.copy2(src_tusk, dst)
            os.chmod(dst, 0o755)
        copied.append(dst.name)

    for src in sorted(source_bin.glob("tusk-*.py")):
        dst = target_bin / src.name
        if not args.dry_run:
            shutil.copy2(src, dst)
        copied.append(dst.name)

    for name in UNDERSCORE_BIN_FILES:
        src = source_bin / name
        if not src.is_file():
            continue
        dst = target_bin / name
        if not args.dry_run:
            shutil.copy2(src, dst)
        copied.append(name)

    if not args.dry_run:
        hash_path = _refresh_lint_hash(target_bin)
        if hash_path is not None:
            copied.append(hash_path.name)

    prefix = "Would copy" if args.dry_run else "Copied"
    print(f"{prefix} {len(copied)} file(s) from {source_bin} -> {target_bin}:")
    for name in copied:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

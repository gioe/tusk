#!/usr/bin/env python3
"""Prepend a versioned CHANGELOG entry with DB-fetched task bullet summaries.

Called by the tusk wrapper:
    tusk changelog-add [--from-version-file] [<version>] [<task_id>...]

The version is sourced in the following order:
    1. --from-version-file flag → read VERSION file (positional args are all task IDs).
    2. First positional arg if numeric → version (existing convention).
    3. No positional args → fall back to VERSION file.

Whichever source is used, the resolved version is cross-checked against the
VERSION file's content; a mismatch is treated as drift and aborts with a clear
error (issue #814 — silent CHANGELOG/VERSION drift after tusk version-bump).

Arguments received from the tusk wrapper:
    sys.argv[1] — repo root
    sys.argv[2] — DB path
    sys.argv[3:] — caller args (parsed by argparse below)

Writes the new entry to CHANGELOG.md immediately after the ## [Unreleased]
heading and outputs the inserted block text to stdout for LLM review.
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection


def fetch_summaries(conn: sqlite3.Connection, task_ids: list[str]) -> list[dict]:
    results = []
    for tid in task_ids:
        row = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (int(tid),)
        ).fetchone()
        if row:
            results.append({"id": row["id"], "summary": row["summary"]})
        else:
            results.append({"id": int(tid), "summary": f"(task {tid} not found)"})
    return results


def _read_version_file(repo_root: str) -> str | None:
    path = os.path.join(repo_root, "VERSION")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tusk changelog-add",
        description=(
            "Prepend a versioned CHANGELOG entry with DB-fetched task bullet summaries. "
            "Version defaults to the VERSION file; an explicit positional version must match."
        ),
        usage="tusk changelog-add [--from-version-file] [<version>] [<task_id>...]",
    )
    parser.add_argument("repo_root")
    parser.add_argument("db_path")
    parser.add_argument(
        "--from-version-file",
        action="store_true",
        help="Force-read version from the VERSION file; positional args are all task IDs.",
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="Optional <version> followed by task IDs. Omit <version> to default to VERSION file.",
    )
    parsed = parser.parse_args()

    repo_root = parsed.repo_root
    db_path = parsed.db_path
    raw_args = list(parsed.args)
    file_version = _read_version_file(repo_root)

    if parsed.from_version_file:
        if file_version is None:
            print(
                f"Error: --from-version-file passed but {os.path.join(repo_root, 'VERSION')} not found",
                file=sys.stderr,
            )
            sys.exit(1)
        version = file_version
        task_ids = raw_args
    elif not raw_args:
        if file_version is None:
            print(
                "Error: no version provided and VERSION file not found.\n"
                "Usage: tusk changelog-add [--from-version-file] [<version>] [<task_id>...]",
                file=sys.stderr,
            )
            sys.exit(1)
        version = file_version
        task_ids = []
    else:
        version = raw_args[0]
        task_ids = raw_args[1:]

    if not version.isdigit() or int(version) == 0:
        print(
            f"Error: version must be a positive integer (got {version!r})",
            file=sys.stderr,
        )
        sys.exit(1)

    if file_version is not None and version != file_version:
        print(
            f"Error: changelog-add version {version!r} disagrees with VERSION file content "
            f"({file_version!r}).\n"
            "This usually means VERSION was bumped after the changelog-add was scripted.\n"
            "If you meant to use the VERSION file value, omit the version arg or pass "
            "--from-version-file.\n"
            "If you meant to override, update the VERSION file first.",
            file=sys.stderr,
        )
        sys.exit(1)

    changelog_path = f"{repo_root}/CHANGELOG.md"
    today = date.today().strftime("%Y-%m-%d")

    bullets: list[str] = []
    if task_ids:
        conn = get_connection(db_path)
        tasks = fetch_summaries(conn, task_ids)
        conn.close()
        for t in tasks:
            bullets.append(f"- [TASK-{t['id']}] {t['summary']}")
    else:
        bullets.append("- (no tasks specified)")

    entry_block = f"## [{version}] - {today}\n\n" + "\n".join(bullets) + "\n"

    with open(changelog_path) as f:
        content = f.read()

    marker = "## [Unreleased]"
    idx = content.find(marker)
    if idx == -1:
        print(f"Error: '{marker}' not found in CHANGELOG.md", file=sys.stderr)
        sys.exit(1)

    eol = content.find("\n", idx)
    if eol == -1:
        eol = len(content) - 1

    new_content = content[: eol + 1] + "\n" + entry_block + content[eol + 1 :]

    with open(changelog_path, "w") as f:
        f.write(new_content)

    subprocess.run(["git", "-C", repo_root, "add", changelog_path], check=True)

    print(entry_block, end="")


if __name__ == "__main__":
    main()

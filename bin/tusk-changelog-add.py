#!/usr/bin/env python3
"""Prepend a versioned CHANGELOG entry with DB-fetched task bullet summaries.

Called by the tusk wrapper:
    tusk changelog-add <version> [<task_id>...]

Arguments received from tusk:
    sys.argv[1] — repo root
    sys.argv[2] — DB path
    sys.argv[3] — version number (integer)
    sys.argv[4:] — task IDs whose summaries become bullet points

Writes the new entry to CHANGELOG.md immediately after the ## [Unreleased]
heading and outputs the inserted block text to stdout for LLM review.
"""

import sqlite3
import subprocess
import sys
from datetime import date


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


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


def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: tusk changelog-add <version> [<task_id>...]", file=sys.stderr)
        sys.exit(1)

    repo_root = sys.argv[1]
    db_path = sys.argv[2]
    version = sys.argv[3]
    task_ids = sys.argv[4:]

    changelog_path = f"{repo_root}/CHANGELOG.md"
    today = date.today().strftime("%Y-%m-%d")

    # Build bullet lines
    bullets: list[str] = []
    if task_ids:
        conn = get_connection(db_path)
        tasks = fetch_summaries(conn, task_ids)
        conn.close()
        for t in tasks:
            bullets.append(f"- [TASK-{t['id']}] {t['summary']}")
    else:
        bullets.append("- (no tasks specified)")

    # Compose entry block (no leading newline — insertion adds one)
    entry_block = f"## [{version}] - {today}\n\n" + "\n".join(bullets) + "\n"

    # Read and update CHANGELOG.md
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

    # Output the inserted block for LLM review
    print(entry_block, end="")


if __name__ == "__main__":
    main()

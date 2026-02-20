#!/usr/bin/env python3
"""Consolidate groom-backlog auto-close pre-checks into a single CLI command.

Called by the tusk wrapper:
    tusk autoclose

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — (unused, reserved for future flags)

Runs all three pre-checks in one call:
  1. Expired deferred tasks → closed_reason = 'expired'
  2. To Do / In Progress tasks with merged PRs → closed_reason = 'completed'
  3. Moot contingent tasks → closed_reason = 'wont_do'

For each closure, appends an annotation to the description and closes open sessions.
Prints a JSON summary with counts per category and closed task IDs.
"""

import json
import sqlite3
import subprocess
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def close_sessions(conn: sqlite3.Connection, task_id: int) -> int:
    """Close all open sessions for a task. Returns number of sessions closed."""
    cursor = conn.execute(
        "UPDATE task_sessions "
        "SET ended_at = datetime('now'), "
        "    duration_seconds = CAST((julianday(datetime('now')) - julianday(started_at)) * 86400 AS INTEGER), "
        "    lines_added = COALESCE(lines_added, 0), "
        "    lines_removed = COALESCE(lines_removed, 0) "
        "WHERE task_id = ? AND ended_at IS NULL",
        (task_id,),
    )
    return cursor.rowcount


def close_task(conn: sqlite3.Connection, task_id: int, reason: str, annotation: str) -> None:
    """Set task to Done with closed_reason and append annotation to description."""
    conn.execute(
        "UPDATE tasks "
        "SET status = 'Done', "
        "    closed_reason = ?, "
        "    updated_at = datetime('now'), "
        "    description = description || char(10) || char(10) || '---' || char(10) || ? "
        "WHERE id = ?",
        (reason, annotation, task_id),
    )


def autoclose_expired_deferred(conn: sqlite3.Connection) -> list[int]:
    """Close deferred tasks past their expiry date. Returns list of closed task IDs."""
    rows = conn.execute(
        "SELECT id FROM tasks "
        "WHERE summary LIKE '%[Deferred]%' "
        "  AND status = 'To Do' "
        "  AND expires_at IS NOT NULL "
        "  AND expires_at < datetime('now')"
    ).fetchall()

    closed_ids = []
    for row in rows:
        task_id = row["id"]
        close_task(
            conn, task_id, "expired",
            "Auto-closed: Deferred task expired after 60 days without action.",
        )
        close_sessions(conn, task_id)
        closed_ids.append(task_id)

    return closed_ids


def check_pr_merged(pr_url: str) -> str | None:
    """Check PR merge status via gh CLI. Returns 'MERGED', 'CLOSED', 'OPEN', or None on error."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state", "--jq", ".state"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def autoclose_merged_prs(conn: sqlite3.Connection) -> dict:
    """Close To Do / In Progress tasks whose PRs are merged. Returns dict with closed IDs and flagged IDs."""
    rows = conn.execute(
        "SELECT id, summary, github_pr FROM tasks "
        "WHERE status IN ('To Do', 'In Progress') "
        "  AND github_pr IS NOT NULL "
        "  AND github_pr <> ''"
    ).fetchall()

    closed_ids = []
    flagged = []  # PRs closed without merge — for user review

    for row in rows:
        task_id = row["id"]
        pr_url = row["github_pr"]
        state = check_pr_merged(pr_url)

        if state == "MERGED":
            close_task(
                conn, task_id, "completed",
                "Auto-closed: PR was already merged.",
            )
            close_sessions(conn, task_id)
            closed_ids.append(task_id)
        elif state == "CLOSED":
            flagged.append({"id": task_id, "summary": row["summary"], "pr": pr_url, "note": "PR closed without merge"})

    return {"closed": closed_ids, "flagged": flagged}


def autoclose_moot_contingent(conn: sqlite3.Connection) -> list[dict]:
    """Close tasks contingent on upstream tasks that closed as wont_do/expired.
    Returns list of dicts with closed task ID and upstream reference."""
    rows = conn.execute(
        "SELECT t.id, t.summary, "
        "       d.depends_on_id AS upstream_id, "
        "       upstream.closed_reason AS upstream_reason "
        "FROM tasks t "
        "JOIN task_dependencies d ON t.id = d.task_id "
        "JOIN tasks upstream ON d.depends_on_id = upstream.id "
        "WHERE t.status <> 'Done' "
        "  AND d.relationship_type = 'contingent' "
        "  AND upstream.status = 'Done' "
        "  AND upstream.closed_reason IN ('wont_do', 'expired')"
    ).fetchall()

    closed = []
    for row in rows:
        task_id = row["id"]
        upstream_id = row["upstream_id"]
        upstream_reason = row["upstream_reason"]

        annotation = f"Auto-closed: Contingent on TASK-{upstream_id} which closed as {upstream_reason}."
        close_task(conn, task_id, "wont_do", annotation)
        close_sessions(conn, task_id)
        closed.append({"id": task_id, "upstream_id": upstream_id, "upstream_reason": upstream_reason})

    return closed


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: tusk autoclose", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path — reserved for future use

    conn = get_connection(db_path)

    # 1. Expired deferred
    expired_ids = autoclose_expired_deferred(conn)

    # 2. Merged PRs
    pr_result = autoclose_merged_prs(conn)

    # 3. Moot contingent
    moot_closed = autoclose_moot_contingent(conn)

    conn.commit()

    # Build summary
    summary = {
        "expired_deferred": {"count": len(expired_ids), "task_ids": expired_ids},
        "merged_prs": {"count": len(pr_result["closed"]), "task_ids": pr_result["closed"]},
        "moot_contingent": {"count": len(moot_closed), "task_ids": [c["id"] for c in moot_closed]},
        "total_closed": len(expired_ids) + len(pr_result["closed"]) + len(moot_closed),
    }

    if pr_result["flagged"]:
        summary["flagged_for_review"] = pr_result["flagged"]

    if moot_closed:
        summary["moot_details"] = moot_closed

    print(json.dumps(summary, indent=2))

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

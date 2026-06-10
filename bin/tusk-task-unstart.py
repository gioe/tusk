#!/usr/bin/env python3
"""tusk task-unstart: Revert a cleanly-orphaned In Progress task back to To Do.

Use when `tusk task-start` has handed back a task that turns out to be
non-actionable (e.g., a chained dep wasn't recorded, so the task isn't actually
ready). The status-transition trigger normally blocks `In Progress -> To Do`
because Done is terminal — this command bypasses that trigger only when the
task is *cleanly orphaned*: no progress checkpoints, no commits referencing
``[TASK-<id>]`` (after a file-overlap prefix-collision check), and no open
session. Partially-worked tasks stay forward-only and must close via
task-done / merge / abandon.

``--close-sessions`` (issue #1043) collapses the routine skip-a-just-started-
task dance to one command: instead of refusing on open sessions, close them
(mirroring ``tusk session-close --task-id``) and revert in the same
transaction as the status UPDATE. It does NOT bypass the progress-checkpoint
or [TASK-<id>] commit-overlap guards — those refuse before sessions are
touched.

Historical [TASK-<id>] commits whose diff has no overlap with the task's
description / criteria paths are treated as prefix-match false positives
(see issue #627) and ignored — the same heuristic tusk-check-deliverables.py
uses to downgrade `merged_not_closed` to `merged_not_closed_low_confidence`.
When there is no scope signal to compare against, the original refusal stands.

Exit codes:
  0  reverted; JSON printed on stdout
  1  --force missing (confirmation hint printed)
  2  task not found, wrong status, or a guard fired (task_progress rows,
     [TASK-<id>] commits with task-scope overlap, or an open session
     without --close-sessions)
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
status_transition_trigger_bypassed = _db_lib.status_transition_trigger_bypassed
find_task_commits = _git_helpers.find_task_commits
filter_commits_by_block_overlap = _git_helpers.filter_commits_by_block_overlap


def _emit_scope_enforced_bypass(task_id: int) -> None:
    """One-line stderr note when the scope_enforced=1 bypass fires.

    Issue/TASK-472: with the commit-time scope guard in place, [TASK-<id>]
    commits are scope-guaranteed by construction — the prefix-collision
    file-overlap heuristic is unnecessary. This note records that the
    bypass path was hit so an operator can confirm the new flow is hot.

    TTY-gated like ``maybe_warn_cross_repo_drift`` (issue #850): silent
    when stderr is not a TTY (agent transcripts, piped logs, CI runs),
    silenced unconditionally by ``TUSK_QUIET=1``, force-emitted via
    ``TUSK_FORCE_WARN=1`` (used by the regression tests).
    """
    if os.environ.get("TUSK_QUIET"):
        return
    if not os.environ.get("TUSK_FORCE_WARN") and not sys.stderr.isatty():
        return
    print(
        f"tusk: note — task-unstart bypassed prefix-collision check for TASK-{task_id} "
        f"(scope_enforced=1; commits are authoritative). "
        f"(TUSK_QUIET=1 to silence)",
        file=sys.stderr,
    )


def _commits_are_prefix_collision(
    task_id: int,
    conn: sqlite3.Connection,
    repo_root: str,
    commits: list,
) -> bool:
    """Return True if `commits` are likely a [TASK-<id>] prefix-match false positive.

    Delegates to the shared block-level scope filter in tusk-git-helpers.py
    (issue #855) with ``fallthrough=False`` so an empty kept set means
    "every block is off-scope" rather than the filter-caller default of
    "no signal — keep all". Centralizes the heuristic that previously
    lived inline as an aggregate intersection: the binary refuse/permit
    decision is invariant to block vs. aggregate grouping, since either
    answers the same "does any commit hit a scope path?" question.

    ``task_paths`` and ``task_basenames`` are intentionally left unset:
    the helper auto-resolves both legs from the DB (issue #670), matching
    the tusk-merge gate's behavior (issue #855 follow-up). Descriptions
    that name a touched file by bare basename only — e.g. ``FULL-RETRO.md``
    matched against a commit touching ``skills/retro/FULL-RETRO.md`` — are
    correctly recognized as in-scope and preserve the existing refusal.

    Conservative on empty signal: returns False when ``commits`` is empty
    or the task has no scope signal — the helper returns ``list(commits)``
    unchanged in the no-signal case regardless of *fallthrough*, so
    ``not kept`` is False and the original refusal stands.
    """
    if not commits:
        return False
    kept = filter_commits_by_block_overlap(
        commits, task_id, repo_root, conn, fallthrough=False
    )
    return not kept


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    parser = argparse.ArgumentParser(
        prog="tusk task-unstart",
        description=(
            "Revert a cleanly-orphaned In Progress task back to To Do. "
            "Refuses if the task has progress checkpoints, an open session "
            "(unless --close-sessions is passed), or [TASK-<id>] commits whose "
            "diff overlaps with files referenced by the task. Historical "
            "[TASK-<id>] commits whose diff has no overlap with task scope "
            "(e.g. left over from a prior task numbering) are treated as "
            "prefix-match false positives and ignored."
        ),
    )
    parser.add_argument("task_id", type=int, help="Task ID")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Confirm the reversal. --force does NOT bypass the [TASK-<id>] "
            "commit-overlap, progress-checkpoint, or open-session guards — "
            "those still refuse when triggered."
        ),
    )
    parser.add_argument(
        "--close-sessions",
        action="store_true",
        help=(
            "Close the task's open session(s) instead of refusing on them, so "
            "skipping a just-started task is one command (issue #1043). The "
            "sessions are closed in the same transaction as the status revert. "
            "Does NOT bypass the progress-checkpoint or [TASK-<id>] "
            "commit-overlap guards — those still refuse before any session is "
            "touched."
        ),
    )
    args = parser.parse_args(argv[2:])
    task_id = args.task_id
    force = args.force

    if not force:
        print(
            f"This will revert task {task_id} from 'In Progress' back to 'To Do', clearing started_at.\n"
            "Refuses if the task has any progress checkpoints, [TASK-<id>] commits, or an open session\n"
            "(pass --close-sessions to close open sessions instead of refusing).\n"
            "Re-run with --force to confirm:\n"
            f"  tusk task-unstart {task_id} --force",
            file=sys.stderr,
        )
        return 1

    # repo_root is two levels up from the DB: tusk/tasks.db -> tusk/ -> repo_root
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))

    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found.", file=sys.stderr)
            return 2

        if task["status"] != "In Progress":
            print(
                f"Error: Task {task_id} is '{task['status']}'. "
                "task-unstart only reverses 'In Progress' -> 'To Do'. "
                "Use task-reopen to reset Done or already-To-Do tasks.",
                file=sys.stderr,
            )
            return 2

        progress_rows = conn.execute(
            "SELECT COUNT(*) FROM task_progress WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        if progress_rows > 0:
            print(
                f"Error: Task {task_id} has {progress_rows} progress checkpoint(s). "
                "Cannot un-start a task with recorded work — close it via task-done or merge.",
                file=sys.stderr,
            )
            return 2

        task_commits = find_task_commits(task_id, repo_root, ["--all"])
        # TASK-472: when scope_enforced=1, the commit-time guard ensured every
        # [TASK-N] commit touched only authorized paths — there are no prefix
        # collisions to filter out. Skip the heuristic and treat any matching
        # commit as authoritative. Legacy tasks (scope_enforced=0) still run
        # the file-overlap check below to discount historical false positives.
        scope_enforced = bool(task["scope_enforced"]) if "scope_enforced" in task.keys() else False
        if task_commits and scope_enforced:
            _emit_scope_enforced_bypass(task_id)
        if task_commits and (
            scope_enforced
            or not _commits_are_prefix_collision(
                task_id, conn, repo_root, task_commits
            )
        ):
            sample = ", ".join(c[:7] for c in task_commits[:3])
            more = f" (+{len(task_commits) - 3} more)" if len(task_commits) > 3 else ""
            print(
                f"Error: Task {task_id} has {len(task_commits)} git commit(s) referencing "
                f"[TASK-{task_id}]: {sample}{more}. "
                "Cannot un-start a task with recorded commits — close it via task-done or merge.",
                file=sys.stderr,
            )
            return 2

        open_sessions = conn.execute(
            "SELECT COUNT(*) FROM task_sessions WHERE task_id = ? AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0]
        if open_sessions > 0 and not args.close_sessions:
            print(
                f"Error: Task {task_id} has {open_sessions} open session(s). "
                f"Re-run with --close-sessions to close them and revert in one call:\n"
                f"  tusk task-unstart {task_id} --force --close-sessions\n"
                "Or run `tusk session-close <session_id>` first, then retry task-unstart.",
                file=sys.stderr,
            )
            return 2

        # Drop validate_status_transition for the duration of the UPDATE so
        # the In Progress -> To Do transition isn't blocked. The helper
        # handles the snapshot/restore/regen-triggers choreography that was
        # duplicated in this script and tusk-task-reopen.py prior to #844.
        # The --close-sessions UPDATE rides in the same BEGIN IMMEDIATE
        # transaction so a failed status revert cannot leave sessions
        # half-closed. The session UPDATE mirrors close_sessions() in
        # tusk-autoclose.py.
        sessions_closed = 0
        with status_transition_trigger_bypassed(conn):
            if open_sessions > 0:
                sessions_closed = conn.execute(
                    "UPDATE task_sessions "
                    "SET ended_at = datetime('now'), "
                    "    duration_seconds = CAST((julianday(datetime('now')) - julianday(started_at)) * 86400 AS INTEGER), "
                    "    lines_added = COALESCE(lines_added, 0), "
                    "    lines_removed = COALESCE(lines_removed, 0) "
                    "WHERE task_id = ? AND ended_at IS NULL",
                    (task_id,),
                ).rowcount
            conn.execute(
                "UPDATE tasks SET status = 'To Do', started_at = NULL, "
                "updated_at = datetime('now') WHERE id = ?",
                (task_id,),
            )

        updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = {
            "task": dict(updated),
            "prior_status": "In Progress",
            "sessions_closed": sessions_closed,
        }
        print(dumps(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-unstart <task_id> --force", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))

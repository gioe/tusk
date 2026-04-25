#!/usr/bin/env python3
"""Close a task without merging — for wont_do / duplicate decisions.

Called by the tusk wrapper:
    tusk abandon <task_id> --reason wont_do|duplicate
                           [--session <session_id>] [--note "..."]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id, --reason, optional --session, --note

Behavior — symmetric with `tusk merge` but without any code merge:

  1. Validate --reason is one of the no-commit closure reasons (wont_do / duplicate).
  2. Auto-detect or validate the session ID, mirroring `tusk merge`.
  3. If a feature/TASK-<id>-* branch exists:
       - Refuse if it has commits not on the default branch (point at `tusk merge`).
       - Otherwise, switch to the default branch and force-delete the branch (-D).
     If no feature branch exists, skip the branch step entirely.
  4. If --note is provided, insert a task_progress row capturing the rationale
     before the task is closed.
  5. Close the open session via `tusk session-close <session_id>`.
  6. Mark the task Done via `tusk task-done <id> --reason <reason> --force`.
     (--force is required because abandoned tasks typically have open criteria
     that the user has intentionally chosen not to complete.)
  7. Print a JSON blob symmetric with `tusk merge` output:
       { "task": {...}, "sessions_closed": N, "unblocked_tasks": [...] }
"""

import json
import os
import subprocess
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config
checkpoint_wal = _db_lib.checkpoint_wal

# Reuse merge helpers so branch / session / default-branch detection stays in
# lockstep with the merge implementation.
_merge = tusk_loader.load("tusk-merge")
find_task_branch = _merge.find_task_branch
detect_default_branch = _merge.detect_default_branch
_autodetect_session = _merge._autodetect_session


# Reasons that map to the no-commit closure path. expired/completed are
# excluded — completed must go through `tusk merge`, and expired is set by
# `tusk autoclose`, not an interactive abandon.
ABANDON_REASONS = ("wont_do", "duplicate")


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=check)


def _print_usage() -> None:
    print(
        "Usage: tusk abandon <task_id> --reason wont_do|duplicate "
        "[--session <session_id>] [--note \"...\"]",
        file=sys.stderr,
    )


def _branch_has_unmerged_commits(branch_name: str, default_branch: str, task_id: int) -> tuple[bool, str | None]:
    """Return (has_unmerged, error_message).

    True iff the feature branch has any commits not reachable from the default
    branch. Mirrors the cherry-pick aware check used by `tusk merge`: we look
    both at git log (commits exclusive to the feature branch) and `git cherry`
    (commits already applied to default by patch ID, which would still appear
    in `git log` but should not block abandon).
    """
    log_result = run(
        ["git", "log", branch_name, "--not", default_branch, "--oneline"],
        check=False,
    )
    if log_result.returncode != 0:
        return True, (
            f"git log {branch_name} --not {default_branch} failed: "
            f"{log_result.stderr.strip()}"
        )

    if not log_result.stdout.strip():
        return False, None

    # Some/all of those commits may be cherry-picks already on default — git
    # cherry compares by patch ID. If every line starts with '-', the branch
    # is effectively merged and safe to abandon.
    cherry = run(["git", "cherry", default_branch, branch_name], check=False)
    if cherry.returncode == 0:
        cherry_lines = [line for line in cherry.stdout.splitlines() if line.strip()]
        if cherry_lines and not any(line.startswith("+ ") for line in cherry_lines):
            return False, None

    return True, None


def _insert_abandon_note(db_path: str, task_id: int, reason: str, note: str) -> None:
    """Persist the abandon rationale as a task_progress row.

    Stored on commit_message so the note shows up in the same field that
    `tusk task-start` uses to brief the next agent. commit_hash and
    files_changed are intentionally NULL — abandoning produces no commit.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO task_progress (task_id, commit_message, next_steps) "
            "VALUES (?, ?, ?)",
            (task_id, f"[abandon: {reason}] {note}", None),
        )
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        _print_usage()
        return 1

    db_path = argv[0]
    config_path = argv[1]

    try:
        task_id = int(argv[2])
    except ValueError:
        print(f"Error: Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    script_dir = os.path.dirname(os.path.abspath(__file__))
    tusk_bin = os.path.join(script_dir, "tusk")

    # Parse remaining flags
    remaining = argv[3:]
    reason: str | None = None
    session_id: int | None = None
    note: str | None = None

    i = 0
    while i < len(remaining):
        if remaining[i] == "--reason":
            if i + 1 >= len(remaining):
                print("Error: --reason requires a value", file=sys.stderr)
                return 1
            reason = remaining[i + 1]
            i += 2
        elif remaining[i] == "--session":
            if i + 1 >= len(remaining):
                print("Error: --session requires a value", file=sys.stderr)
                return 1
            try:
                session_id = int(remaining[i + 1])
            except ValueError:
                print(f"Error: Invalid session ID: {remaining[i + 1]}", file=sys.stderr)
                return 1
            i += 2
        elif remaining[i] == "--note":
            if i + 1 >= len(remaining):
                print("Error: --note requires a value", file=sys.stderr)
                return 1
            note = remaining[i + 1]
            i += 2
        else:
            print(f"Error: Unknown argument: {remaining[i]}", file=sys.stderr)
            return 1

    if reason is None:
        print("Error: --reason wont_do|duplicate is required", file=sys.stderr)
        _print_usage()
        return 1

    if reason not in ABANDON_REASONS:
        print(
            f"Error: --reason must be one of {'|'.join(ABANDON_REASONS)} "
            f"(got '{reason}'). Use `tusk merge` for completed tasks.",
            file=sys.stderr,
        )
        return 1

    # Cross-check against config-defined closed_reasons so a project that has
    # narrowed the allowed set still gets a clear early failure rather than a
    # confusing downstream task-done error.
    valid_reasons = load_config(config_path).get("closed_reasons", [])
    if valid_reasons and reason not in valid_reasons:
        print(
            f"Error: '{reason}' is not in this project's closed_reasons "
            f"({', '.join(valid_reasons)}).",
            file=sys.stderr,
        )
        return 1

    # Validate explicit session like merge does: warn + fall back to autodetect
    # if it doesn't belong to the task or is already closed.
    if session_id is not None:
        try:
            conn = get_connection(db_path)
            try:
                row = conn.execute(
                    "SELECT id FROM task_sessions "
                    "WHERE id = ? AND task_id = ? AND ended_at IS NULL",
                    (session_id, task_id),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"Error: Could not query sessions: {e}", file=sys.stderr)
            return 1

        if row is None:
            print(
                f"Warning: Session {session_id} is not an open session for "
                f"task {task_id}; falling back to auto-detection.",
                file=sys.stderr,
            )
            session_id = None

    if session_id is None:
        session_id, err_code = _autodetect_session(db_path, task_id, tusk_bin)
        if err_code is not None:
            return err_code

    # Branch safety: refuse if the feature branch carries commits the user
    # would lose. This is the whole reason `abandon` exists as its own command —
    # it's the no-commit version of `merge`.
    branch_name, branch_err, pre_merged = find_task_branch(task_id)

    if pre_merged:
        # User is already on the default branch with no feature branch in
        # sight. Nothing to delete; proceed straight to close.
        branch_name = None
        branch_err = None

    if branch_err and not pre_merged:
        # No feature branch found, AND we are not on the default branch. That
        # is fine for abandon — there is simply nothing to clean up. Treat as
        # "no branch" rather than an error.
        if "No branch found matching" in branch_err:
            branch_name = None
        else:
            print(f"Error: {branch_err}", file=sys.stderr)
            return 1

    default_branch = detect_default_branch()

    if branch_name:
        has_unmerged, log_err = _branch_has_unmerged_commits(
            branch_name, default_branch, task_id
        )
        if log_err:
            print(f"Error: {log_err}", file=sys.stderr)
            return 1
        if has_unmerged:
            print(
                f"Error: feature branch '{branch_name}' has commits not on "
                f"'{default_branch}'. Refusing to abandon — use `tusk merge "
                f"{task_id}` to ship the work, or delete the branch manually "
                f"with `git branch -D {branch_name}` first if you really want "
                "to discard it.",
                file=sys.stderr,
            )
            return 2

    # WAL checkpoint before any DB writes / branch swaps for the same reason
    # `tusk merge` does it: a subsequent branch switch can revert tasks.db
    # to a pre-WAL snapshot otherwise.
    checkpoint_wal(db_path)

    # Persist the abandon rationale before we close anything so the audit
    # trail survives even if a downstream step fails partway.
    if note:
        try:
            _insert_abandon_note(db_path, task_id, reason, note)
        except sqlite3.Error as e:
            print(
                f"Warning: Could not record abandon note: {e}",
                file=sys.stderr,
            )

    # Switch off the feature branch and delete it. Use -D because the branch
    # is not merged into default — that's the whole point of abandon.
    if branch_name:
        current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
        on_feature = (
            current.returncode == 0
            and current.stdout.strip() == branch_name
        )
        if on_feature:
            checkout = run(["git", "checkout", default_branch], check=False)
            if checkout.returncode != 0:
                print(
                    f"Error: git checkout {default_branch} failed:\n"
                    f"{checkout.stderr.strip()}",
                    file=sys.stderr,
                )
                return 2

        delete = run(["git", "branch", "-D", branch_name], check=False)
        if delete.returncode != 0:
            # Non-fatal: the branch may already be gone, but warn so the
            # user isn't surprised by lingering refs.
            print(
                f"Warning: git branch -D {branch_name} failed:\n"
                f"{delete.stderr.strip()}",
                file=sys.stderr,
            )

    # Close the session (mirrors tusk merge step 2)
    print(f"Closing session {session_id}...", file=sys.stderr)
    sc = run([tusk_bin, "session-close", str(session_id)], check=False)
    session_was_closed = sc.returncode == 0
    if sc.returncode != 0:
        if "already closed" in sc.stderr or "No session found" in sc.stderr:
            print(f"Warning: {sc.stderr.strip()}", file=sys.stderr)
        else:
            print(
                f"Error: session-close failed:\n{sc.stderr.strip()}",
                file=sys.stderr,
            )
            return 2

    # Mark the task Done. Always pass --force because abandoned tasks
    # typically have open criteria the user has decided not to complete.
    print(f"Closing task {task_id}...", file=sys.stderr)
    td = run(
        [tusk_bin, "task-done", str(task_id), "--reason", reason, "--force"],
        check=False,
    )
    if td.returncode != 0:
        print(f"Error: task-done failed:\n{td.stderr.strip()}", file=sys.stderr)
        return 2

    try:
        task_done_result = json.loads(td.stdout)
    except json.JSONDecodeError:
        if td.stdout.strip():
            print(td.stdout.strip())
        return 0

    # Mirror tusk merge's behavior: task-done sees 0 open sessions because
    # session-close already ran, so correct the counter for our caller.
    if session_was_closed:
        task_done_result["sessions_closed"] = 1

    print(dumps(task_done_result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print(
            "Error: This script must be invoked via the tusk wrapper.",
            file=sys.stderr,
        )
        _print_usage()
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Finalize a task: close session, merge branch, push, clean up, and close task.

Called by the tusk wrapper:
    tusk merge <task_id> [--session <session_id>] [--pr] [--pr-number N]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id [--session <session_id>] [--pr] [--pr-number N]

If --session is omitted, the open session for the task is auto-detected:
  - Exactly one open session → use it
  - No open sessions, but closed one exists → use most-recent closed session (warning)
  - No open sessions, no closed sessions → error with helpful message
  - Multiple open sessions → error listing all of them

Default behavior (merge.mode = local):
  1. Preflight: verify working tree is clean and feature branch exists (errors here leave session and task untouched)
  2. tusk session-close <session_id> (captures diff stats before branch change)
  3. git checkout <default_branch> && git pull
  4. git merge --ff-only feature/TASK-<id>-*
  5. git push
  6. git branch -d feature/TASK-<id>-*
  7. tusk task-done <id> --reason completed (--force if task-done warns)
  8. Print JSON with task details and unblocked_tasks array

--pr flag (or merge.mode = pr in config):
  Replaces steps 3-6 with: gh pr merge <pr_number> --squash --delete-branch
  Requires --pr-number.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys


def _load_db_lib():
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk-db-lib.py")
    _s = importlib.util.spec_from_file_location("tusk_db_lib", _p)
    _m = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(_m)
    return _m


_db_lib = _load_db_lib()
get_connection = _db_lib.get_connection


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def detect_default_branch() -> str:
    """Detect the repo's default branch via remote HEAD, gh fallback, then 'main'."""
    run(["git", "remote", "set-head", "origin", "--auto"], check=False)
    result = run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().replace("refs/remotes/origin/", "")

    result = run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return "main"


def load_merge_mode(config_path: str) -> str:
    """Load merge.mode from config, defaulting to 'local'."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        return config.get("merge", {}).get("mode", "local")
    except (FileNotFoundError, json.JSONDecodeError):
        return "local"


def find_task_branch(task_id: int) -> tuple[str | None, str | None]:
    """Return (branch_name, error_message). branch_name is None on error."""
    result = run(["git", "branch", "--list", f"feature/TASK-{task_id}-*"], check=False)
    if result.returncode != 0:
        return None, f"git branch --list failed: {result.stderr.strip()}"

    branches = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("* "):
            stripped = stripped[2:]
        if stripped:
            branches.append(stripped)

    if len(branches) == 0:
        return None, f"No branch found matching feature/TASK-{task_id}-*"
    if len(branches) > 1:
        names = ", ".join(branches)
        return None, (
            f"Multiple branches found for TASK-{task_id}: {names}. "
            "Delete all but one before running tusk merge."
        )
    return branches[0], None


def _autodetect_session(
    db_path: str, task_id: int, tusk_bin: str
) -> tuple[int | None, int | None]:
    """Find the session to use for task_id when no explicit session was given.

    Returns (session_id, exit_code). On success exit_code is None.
    On error, session_id is None and exit_code is a non-zero int.
    Prints warnings/errors to stderr.
    """
    try:
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT id, started_at FROM task_sessions WHERE task_id = ? AND ended_at IS NULL ORDER BY id",
                (task_id,),
            ).fetchall()
            if len(rows) == 0:
                closed_rows = conn.execute(
                    "SELECT id FROM task_sessions WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchall()
            else:
                closed_rows = []
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"Error: Could not query sessions: {e}", file=sys.stderr)
        return None, 1

    if len(rows) == 0:
        if len(closed_rows) == 0:
            # No sessions at all. If a feature branch exists, tasks.db was likely reverted
            # by a git stash or checkout — create a synthetic session so merge can proceed.
            branch_check, _ = find_task_branch(task_id)
            if branch_check:
                print(
                    f"Warning: No session found for task {task_id} — tasks.db may have been "
                    "reverted by a git stash or checkout. Creating a synthetic session to "
                    "allow merge to proceed.\n"
                    "Tip: add the tusk database to your .gitignore (run `tusk path` to find "
                    "the exact path) to prevent this in future.",
                    file=sys.stderr,
                )
                result = run([tusk_bin, "task-start", str(task_id), "--force"], check=False)
                if result.returncode != 0:
                    print(
                        f"Error: Could not create synthetic session:\n{result.stderr.strip()}\n\n"
                        "Manual recovery:\n"
                        f"  git checkout <default_branch>\n"
                        f"  git merge --ff-only feature/TASK-{task_id}-*\n"
                        f"  git push\n"
                        f"  tusk task-done {task_id} --reason completed",
                        file=sys.stderr,
                    )
                    return None, 1
                try:
                    start_data = json.loads(result.stdout)
                    session_id = start_data["session_id"]
                    print(f"Synthetic session {session_id} created.", file=sys.stderr)
                except (json.JSONDecodeError, KeyError) as e:
                    print(
                        f"Error: Could not parse session from task-start output: {e}",
                        file=sys.stderr,
                    )
                    return None, 1
            else:
                print(
                    f"Error: No session found for task {task_id}. "
                    "Start a session with `tusk task-start` or pass --session <id> explicitly.",
                    file=sys.stderr,
                )
                return None, 1
        else:
            session_id = closed_rows[0][0]
            print(
                f"Warning: No open session found for task {task_id}; "
                f"falling back to last closed session {session_id}.",
                file=sys.stderr,
            )
    else:
        session_id = rows[0][0]
        print(f"Auto-detected session {session_id} for task {task_id}.", file=sys.stderr)

    return session_id, None


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk merge <task_id> [--session <session_id>] [--pr] [--pr-number N]",
            file=sys.stderr,
        )
        return 1

    # DB path is used for read-only session lookup (auto-detect); write ops
    # (session-close, task-done) are delegated to tusk subprocesses.
    _db_path = argv[0]
    config_path = argv[1]

    try:
        task_id = int(argv[2])
    except ValueError:
        print(f"Error: Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    # Locate the tusk binary (sibling of this script in the same bin/ directory)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tusk_bin = os.path.join(script_dir, "tusk")

    # Parse remaining flags
    remaining = argv[3:]
    session_id = None
    use_pr = False
    pr_number = None

    i = 0
    while i < len(remaining):
        if remaining[i] == "--session":
            if i + 1 >= len(remaining):
                print("Error: --session requires a value", file=sys.stderr)
                return 1
            try:
                session_id = int(remaining[i + 1])
            except ValueError:
                print(f"Error: Invalid session ID: {remaining[i + 1]}", file=sys.stderr)
                return 1
            i += 2
        elif remaining[i] == "--pr":
            use_pr = True
            i += 1
        elif remaining[i] == "--pr-number":
            if i + 1 >= len(remaining):
                print("Error: --pr-number requires a value", file=sys.stderr)
                return 1
            try:
                pr_number = int(remaining[i + 1])
            except ValueError:
                print(f"Error: Invalid PR number: {remaining[i + 1]}", file=sys.stderr)
                return 1
            i += 2
        else:
            print(f"Error: Unknown argument: {remaining[i]}", file=sys.stderr)
            return 1

    # Validate an explicitly-provided session ID. If the session is not found or
    # does not belong to this task, emit a warning and fall back to auto-detection
    # so that any other open session for the task can still be used.
    if session_id is not None:
        try:
            _conn = get_connection(_db_path)
            try:
                _row = _conn.execute(
                    "SELECT id FROM task_sessions WHERE id = ? AND task_id = ? AND ended_at IS NULL",
                    (session_id, task_id),
                ).fetchone()
            finally:
                _conn.close()
        except sqlite3.Error as e:
            print(f"Error: Could not query sessions: {e}", file=sys.stderr)
            return 1

        if _row is None:
            # Produce a specific warning: distinguish "not found", "closed", and "wrong task".
            try:
                _conn_detail = get_connection(_db_path)
                try:
                    _detail_row = _conn_detail.execute(
                        "SELECT task_id, ended_at FROM task_sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                finally:
                    _conn_detail.close()
            except sqlite3.Error:
                _detail_row = None

            if _detail_row is None:
                _reason = f"Session {session_id} not found in database"
            elif _detail_row[1] is not None:
                _reason = f"Session {session_id} is already closed"
            else:
                _reason = f"Session {session_id} belongs to a different task"
            print(
                f"Warning: {_reason}; "
                "falling back to auto-detecting an open session for the task.",
                file=sys.stderr,
            )
            session_id = None

    if session_id is None:
        session_id, err_code = _autodetect_session(_db_path, task_id, tusk_bin)
        if err_code is not None:
            return err_code

    # Resolve merge mode (config can force PR mode)
    merge_mode = load_merge_mode(config_path)
    if merge_mode == "pr":
        use_pr = True

    if use_pr and pr_number is None:
        print("Error: --pr-number <N> is required when using PR mode", file=sys.stderr)
        return 1

    # Preflight checks — abort before touching session or task state
    # Step 1a: Detect feature branch
    branch_name, err = find_task_branch(task_id)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    # Step 1b (local mode only): Abort if working tree is dirty
    # Only check for staged/unstaged changes to tracked files; untracked files are not
    # uncommitted changes and should not block a merge.
    # tasks.db (and its WAL/SHM siblings) are excluded from this check because
    # they are always modified during an active tusk session and are committed
    # as part of normal task workflow — not manually staged before merge.
    if not use_pr:
        unstaged = run(["git", "diff", "--name-only"], check=False)
        staged = run(["git", "diff", "--cached", "--name-only"], check=False)
        if unstaged.returncode != 0 or staged.returncode != 0:
            err = unstaged.stderr.strip() or staged.stderr.strip()
            print(f"Error: git diff failed:\n{err}", file=sys.stderr)
            return 1
        dirty_files = list(dict.fromkeys(
            f
            for f in unstaged.stdout.splitlines() + staged.stdout.splitlines()
            if f and not f.startswith("tusk/tasks.db")
        ))
        if dirty_files:
            print(
                "Error: Working tree has uncommitted changes — cannot proceed with merge.\n"
                "Please stash or commit your changes first:\n"
                "  git stash        # stash and restore later\n"
                "  git stash pop    # restore after merge\n"
                "  git add . && git commit -m 'wip'   # commit as work-in-progress",
                file=sys.stderr,
            )
            return 1

    print(f"Found branch: {branch_name}", file=sys.stderr)

    # Step 2: Close the session (captures git diff stats while on feature branch)
    #
    # Checkpoint the WAL first so that any uncommitted writes (e.g. from tusk task-start)
    # are flushed to the main db file before session-close reads the session row.
    # Without this, a git stash or branch switch that reverts tasks.db to a pre-WAL
    # snapshot can silently drop the session row, causing "No session found" below.
    print("Checkpointing WAL...", file=sys.stderr)
    try:
        _conn = get_connection(_db_path)
        try:
            row = _conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
        finally:
            _conn.close()
        if row and row[0] > 0:
            print(
                f"Warning: WAL checkpoint partially blocked (busy={row[0]}, log={row[1]}, checkpointed={row[2]}) — session row may still be at risk.",
                file=sys.stderr,
            )
    except sqlite3.Error as e:
        # Non-fatal: warn and continue — session-close will surface any real issue.
        print(f"Warning: WAL checkpoint failed: {e} — continuing.", file=sys.stderr)

    print(f"Closing session {session_id}...", file=sys.stderr)
    result = run([tusk_bin, "session-close", str(session_id)], check=False)
    session_was_closed = result.returncode == 0
    if result.returncode != 0:
        if "already closed" in result.stderr:
            print(f"Warning: session {session_id} is already closed — continuing.", file=sys.stderr)
        elif "No session found" in result.stderr:
            # The session row is missing despite the WAL checkpoint above — likely lost
            # due to a git stash/checkout that reverted tasks.db before the WAL was
            # checkpointed. Skip session-close and continue so the merge itself is not
            # blocked by this transient data-loss scenario.
            print(
                f"Warning: session {session_id} not found in DB (may have been lost to a "
                "WAL revert) — skipping session-close and continuing with merge.",
                file=sys.stderr,
            )
        else:
            print(f"Error: session-close failed:\n{result.stderr.strip()}", file=sys.stderr)
            return 2

    if use_pr:
        # PR mode: delegate to gh pr merge
        print(f"Merging PR #{pr_number} via gh...", file=sys.stderr)
        result = run(
            ["gh", "pr", "merge", str(pr_number), "--squash", "--delete-branch"],
            check=False,
        )
        if result.returncode != 0:
            print(f"Error: gh pr merge failed:\n{result.stderr.strip()}", file=sys.stderr)
            return 2
        if result.stdout.strip():
            print(result.stdout.strip(), file=sys.stderr)
    else:
        # Local mode: ff-only merge
        default_branch = detect_default_branch()
        print(f"Merging {branch_name} into {default_branch} (ff-only)...", file=sys.stderr)

        # Step 3: Checkout default branch
        # tasks.db (and WAL/SHM siblings) are gitignored and untracked, so git
        # refuses to overwrite them during checkout.  Move them aside first, then
        # restore after the checkout succeeds.
        db_siblings = [_db_path, _db_path + "-wal", _db_path + "-shm"]
        db_tmp = [p + ".merge-tmp" for p in db_siblings]
        moved = []
        for src, dst in zip(db_siblings, db_tmp):
            if os.path.exists(src):
                os.rename(src, dst)
                moved.append((src, dst))

        result = run(["git", "checkout", default_branch], check=False)
        if result.returncode != 0:
            for src, dst in moved:
                os.rename(dst, src)
            print(
                f"Error: git checkout {default_branch} failed:\n{result.stderr.strip()}",
                file=sys.stderr,
            )
            return 2

        # Restore db files after successful checkout
        for src, dst in moved:
            os.rename(dst, src)

        # Step 4: Pull latest
        result = run(["git", "pull", "origin", default_branch], check=False)
        if result.returncode != 0:
            print(f"Error: git pull failed:\n{result.stderr.strip()}", file=sys.stderr)
            # Restore feature branch so user can investigate
            run(["git", "checkout", branch_name], check=False)
            return 2

        # Step 4 (cont): Fast-forward merge
        result = run(["git", "merge", "--ff-only", branch_name], check=False)
        if result.returncode != 0:
            print(
                f"Error: git merge --ff-only {branch_name} failed:\n{result.stderr.strip()}\n"
                "The feature branch cannot be fast-forward merged. "
                "Rebase the branch onto the default branch first, "
                "or use --pr mode for a squash merge.",
                file=sys.stderr,
            )
            # Restore feature branch so user can investigate
            run(["git", "checkout", branch_name], check=False)
            return 2

        # Step 5: Push
        result = run(["git", "push", "origin", default_branch], check=False)
        if result.returncode != 0:
            print(
                f"Error: git push failed:\n{result.stderr.strip()}\n"
                f"The branch has been merged locally but not pushed.\n"
                f"  Retry: git push origin {default_branch}\n"
                f"  Undo:  git reset --hard HEAD~1 && git checkout {branch_name}",
                file=sys.stderr,
            )
            return 2

        # Step 6: Delete feature branch
        result = run(["git", "branch", "-d", branch_name], check=False)
        if result.returncode != 0:
            # Non-fatal: branch is already merged, warn and continue
            print(
                f"Warning: git branch -d {branch_name} failed:\n{result.stderr.strip()}",
                file=sys.stderr,
            )

    # Step 7: Close the task — run without --force first to surface any warnings
    print(f"Closing task {task_id}...", file=sys.stderr)
    result = run(
        [tusk_bin, "task-done", str(task_id), "--reason", "completed"],
        check=False,
    )
    if result.returncode == 3:
        # task-done has warnings (uncompleted criteria or missing commit hashes);
        # print them so the user is aware, then close with --force
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        result = run(
            [tusk_bin, "task-done", str(task_id), "--reason", "completed", "--force"],
            check=False,
        )
    if result.returncode != 0:
        print(f"Error: task-done failed:\n{result.stderr.strip()}", file=sys.stderr)
        return 2

    # Step 8: Forward the task-done JSON to stdout
    try:
        task_done_result = json.loads(result.stdout)
    except json.JSONDecodeError:
        # task-done produced non-JSON output; print as-is
        if result.stdout.strip():
            print(result.stdout.strip())
        return 0

    # tusk session-close already closed the session before task-done ran, so
    # task-done always sees 0 open sessions. Correct the counter here.
    # When session-close returned non-zero with "already closed", session_was_closed
    # is False and sessions_closed stays 0 — accurate, since this invocation did not
    # close anything. merge always operates on exactly one session_id.
    if session_was_closed:
        task_done_result["sessions_closed"] = 1  # merge always operates on one session

    print(json.dumps(task_done_result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

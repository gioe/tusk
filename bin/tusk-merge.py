#!/usr/bin/env python3
"""Finalize a task: close session, merge branch, push, clean up, and close task.

Called by the tusk wrapper:
    tusk merge <task_id> [--session <session_id>] [--pr] [--pr-number N] [--rebase]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id [--session <session_id>] [--pr] [--pr-number N] [--rebase]

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

import json
import os
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
checkpoint_wal = _db_lib.checkpoint_wal

_git_helpers = tusk_loader.load("tusk-git-helpers")
_is_remote_unreachable = _git_helpers._is_remote_unreachable
_UNREACHABLE_REMOTE_PATTERNS = _git_helpers._UNREACHABLE_REMOTE_PATTERNS
_UNREACHABLE_REMOTE_REGEX = _git_helpers._UNREACHABLE_REMOTE_REGEX
task_grep_arg = _git_helpers.task_grep_arg
find_task_commits = _git_helpers.find_task_commits
commit_changed_files = _git_helpers.commit_changed_files
task_referenced_paths = _git_helpers.task_referenced_paths
iter_branch_auto_stashes = _git_helpers.iter_branch_auto_stashes
_GENERATED_LOCKFILES = _git_helpers.GENERATED_LOCKFILES

_WORKSPACE_NOT_PROVIDED = object()


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=check)


def _run_tusk_subcommand(tusk_bin: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run a project-local tusk subcommand with a targeted transient-missing diagnostic."""
    cmd = [tusk_bin, *args]
    for attempt in (1, 2):
        try:
            return run(cmd, check=False)
        except FileNotFoundError as exc:
            if attempt == 1:
                time.sleep(0.2)
                continue
            message = (
                "project-local tusk binary disappeared during closeout; "
                "retry after any install or upgrade finishes.\n"
                f"Missing executable: {tusk_bin}\n"
                f"Original error: {exc}"
            )
            return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=message)


def _resolve_stable_tusk_bin(db_path: str, fallback: str) -> str:
    """Resolve a primary-install tusk binary that survives task-worktree cleanup.

    When tusk merge is invoked from a task worktree (the recommended workflow),
    ``__file__`` lives inside that worktree's ``.claude/bin/`` — but the no-checkout
    cleanup step deletes the worktree mid-flow, invalidating any ``__file__``-derived
    binary path used for the subsequent session-close / task-done subprocess calls
    (issue #834). Resolve to the primary checkout's binary instead, probing both
    the Claude (``.claude/bin/tusk``) and Codex (``tusk/bin/tusk``) layouts. Falls
    back to ``fallback`` when neither is present, preserving test-environment
    behavior where the DB lives outside a real install tree.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    for candidate in (
        os.path.join(repo_root, ".claude", "bin", "tusk"),
        os.path.join(repo_root, "tusk", "bin", "tusk"),
    ):
        if os.path.exists(candidate) and os.path.realpath(candidate) != os.path.realpath(fallback):
            return candidate
    return fallback


_INDEX_LOCK_RE = re.compile(r"Unable to create '[^']*\.git/index\.lock'")


def _run_with_index_lock_retry(
    cmd: list[str], label: str, sleep_seconds: float = 0.5
) -> subprocess.CompletedProcess:
    """Run `cmd`; retry once after a short sleep when the failure is a
    transient `.git/index.lock` contention (issues #620, #640).

    Other failures are returned immediately with no sleep, preserving the
    original behavior for non-transient errors. `label` is used in the retry
    notice on stderr.
    """
    result = run(cmd, check=False)
    if result.returncode == 0 or not _INDEX_LOCK_RE.search(result.stderr or ""):
        return result
    print(
        f"{label}: transient .git/index.lock contention; retrying once...",
        file=sys.stderr,
    )
    time.sleep(sleep_seconds)
    return run(cmd, check=False)


def _has_remote(name: str = "origin") -> bool:
    """Return True if the named git remote exists."""
    result = run(["git", "remote", "get-url", name], check=False)
    return result.returncode == 0


def _is_default_branch_locked_by_worktree(stderr: str, default_branch: str) -> bool:
    """Return True for git's "branch is already used by worktree" checkout error."""
    quoted = re.escape(default_branch)
    pattern = rf"fatal: '?{quoted}'? is already used by worktree at "
    return re.search(pattern, stderr or "") is not None


def _worktree_path_for_branch(branch: str) -> str | None:
    """Return another worktree path currently checking out ``branch``, if any."""
    current = run(["git", "rev-parse", "--show-toplevel"], check=False)
    current_path = os.path.realpath(current.stdout.strip()) if current.returncode == 0 else None

    result = run(["git", "worktree", "list", "--porcelain"], check=False)
    if result.returncode != 0:
        return None

    listed_path = None
    expected_ref = f"refs/heads/{branch}"
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("worktree "):
            listed_path = line[len("worktree "):]
            continue
        if line == f"branch {expected_ref}" and listed_path:
            if current_path and os.path.realpath(listed_path) == current_path:
                continue
            return listed_path
    return None


def _local_default_unpushed_commits(default_branch: str) -> list[tuple[str, str]] | None:
    """Return [(sha, subject), ...] for commits on local <default_branch> that are
    not yet on origin/<default_branch>.

    Returns None when the comparison can't be performed (e.g. origin/<default> ref
    is missing because the repo has never fetched). Empty list means nothing unpushed.
    """
    rev_parse = run(
        ["git", "rev-parse", "--verify", f"refs/remotes/origin/{default_branch}"],
        check=False,
    )
    if rev_parse.returncode != 0:
        return None
    log = run(
        ["git", "log", "--format=%h %s",
         f"refs/remotes/origin/{default_branch}..{default_branch}"],
        check=False,
    )
    if log.returncode != 0:
        return None
    commits: list[tuple[str, str]] = []
    for raw in log.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        commits.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return commits


def _confirm_proceed_with_unpushed(
    commits: list[tuple[str, str]], default_branch: str, task_id: int
) -> bool:
    """Surface unpushed commits on local default and ask whether to push them
    alongside this merge, abort, or drop them.

    Returns True to proceed with the merge, False to abort. The 'd' (drop) branch
    runs `git fetch origin && git reset --hard origin/<default>` after the user
    types the full word 'drop' to confirm, then returns False so the caller
    re-runs `tusk merge` against the now-clean default.

    In a non-interactive context (no TTY on stdin) always returns False — silently
    shipping unaudited commits is the bug this guard exists to prevent (issue #607).
    """
    print(
        f"\nWarning: local '{default_branch}' is ahead of "
        f"'origin/{default_branch}' by {len(commits)} commit(s) that did not "
        f"come from this feature branch. They would be pushed as silent "
        f"passengers of TASK-{task_id} if the merge proceeds:",
        file=sys.stderr,
    )
    for sha, subject in commits:
        print(f"  {sha}  {subject}", file=sys.stderr)

    if not sys.stdin.isatty():
        print(
            f"\nAborting: refusing to push the commits above as part of "
            f"TASK-{task_id} without an interactive confirmation. To resolve:\n"
            f"  - Push them yourself first: git push origin {default_branch}\n"
            f"  - Or drop them: git fetch origin && "
            f"git reset --hard origin/{default_branch}\n"
            f"Then re-run: tusk merge {task_id}",
            file=sys.stderr,
        )
        return False

    print(
        f"\nProceed with this TASK-{task_id} merge?\n"
        f"  [y] push the commits above as part of this merge\n"
        f"  [n] abort (default)\n"
        f"  [d] drop the commits — runs git fetch origin && "
        f"git reset --hard origin/{default_branch}\n"
        f"[y/n/d] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = sys.stdin.readline().strip().lower()
    if answer in ("y", "yes"):
        return True
    if answer == "d":
        return _drop_unpushed_commits(commits, default_branch, task_id)
    print(
        f"Aborting. To resolve manually:\n"
        f"  - Push them: git push origin {default_branch}\n"
        f"  - Drop them: git fetch origin && "
        f"git reset --hard origin/{default_branch}\n"
        f"Then re-run: tusk merge {task_id}",
        file=sys.stderr,
    )
    return False


def _drop_unpushed_commits(
    commits: list[tuple[str, str]], default_branch: str, task_id: int
) -> bool:
    """Run the destructive drop path after a typed-word confirmation.

    Requires the user to type the full word 'drop' (case-insensitive) before
    invoking `git fetch origin && git reset --hard origin/<default>`. Always
    returns False — even on success the merge does not proceed; the caller
    re-runs `tusk merge` against the now-clean default branch.
    """
    print(
        f"\nThis will run: git fetch origin && "
        f"git reset --hard origin/{default_branch}\n"
        f"It will permanently discard the {len(commits)} unpushed commit(s) above "
        f"on local '{default_branch}'.\n"
        f"Type 'drop' to confirm (anything else aborts): ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    confirmation = sys.stdin.readline().strip()
    if confirmation.lower() != "drop":
        print(
            f"Aborted — typed {confirmation!r}, expected 'drop'. "
            f"No changes made.",
            file=sys.stderr,
        )
        return False

    fetch = run(["git", "fetch", "origin"], check=False)
    if fetch.returncode != 0:
        print(
            f"Aborted — 'git fetch origin' failed:\n{fetch.stderr}",
            file=sys.stderr,
        )
        return False

    reset = run(
        ["git", "reset", "--hard", f"origin/{default_branch}"], check=False
    )
    if reset.returncode != 0:
        print(
            f"Aborted — 'git reset --hard origin/{default_branch}' failed:\n"
            f"{reset.stderr}",
            file=sys.stderr,
        )
        return False

    head = run(["git", "rev-parse", "HEAD"], check=False)
    new_head = head.stdout.strip() if head.returncode == 0 else "(unknown)"
    print(
        f"Dropped {len(commits)} unpushed commit(s) on local "
        f"'{default_branch}'. HEAD is now {new_head}.\n"
        f"Re-run: tusk merge {task_id}",
        file=sys.stderr,
    )
    return False


def _try_pop_stash(task_id: int) -> None:
    """Attempt to pop the auto-stash created before merging and report the outcome.

    Locates the stash entry by its label ('tusk-merge: auto-stash for TASK-N') and
    pops it by explicit index so that stash entries pushed by hooks or other tools
    between the auto-stash and the pop do not get accidentally restored.

    If the pop fails only because of conflicts in known generated lockfiles, those
    conflicts are auto-resolved by preferring the stash (WIP) version and the stash
    entry is dropped.  If the conflict includes any non-generated file, the usual
    warning + manual-restore instruction is printed instead.
    """
    label = f"tusk-merge: auto-stash for TASK-{task_id}"
    # Defensive: skip when there are no stashes at all. Callers already gate
    # on `did_stash`, but the rev-parse check makes the function safe to call
    # in isolation and avoids a spurious `git stash list` invocation when the
    # repo has no stashes (issue #658).
    if run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/stash"], check=False
    ).returncode != 0:
        print(
            f"Warning: could not find auto-stash entry '{label}' — "
            "run 'git stash list' and restore your changes manually.",
            file=sys.stderr,
        )
        return
    stash_list = run(["git", "stash", "list"], check=False)
    stash_index: int | None = None
    found_line = False
    if stash_list.returncode == 0:
        for line in stash_list.stdout.splitlines():
            # Lines look like: "stash@{N}: On branch: <message>"
            # Use endswith — substring `in` would false-positive on TASK-id
            # prefix collisions (e.g. TASK-2 matching a TASK-29 entry).
            if line.startswith("stash@{") and line.rstrip().endswith(label):
                found_line = True
                try:
                    stash_index = int(line.split("{")[1].split("}")[0])
                except (IndexError, ValueError):
                    pass
                break

    if stash_index is None:
        msg = (
            f"Warning: could not parse stash index for entry '{label}'"
            if found_line
            else f"Warning: could not find auto-stash entry '{label}'"
        )
        print(
            msg + " — run 'git stash list' and restore your changes manually.",
            file=sys.stderr,
        )
        return

    stash_ref = f"stash@{{{stash_index}}}"
    pop = run(["git", "stash", "pop", stash_ref], check=False)
    if pop.returncode == 0:
        print(
            "Note: stash restored to working tree — you do not need to run git stash pop.",
            file=sys.stderr,
        )
        return

    # Pop failed — check whether all conflicts are in generated lockfiles.
    conflicts_result = run(["git", "diff", "--name-only", "--diff-filter=U"], check=False)
    conflicted: list[str] = []
    if conflicts_result.returncode == 0 and conflicts_result.stdout.strip():
        conflicted = [f for f in conflicts_result.stdout.splitlines() if f]

    if conflicted:
        generated = [f for f in conflicted if os.path.basename(f) in _GENERATED_LOCKFILES]
        non_generated = [f for f in conflicted if os.path.basename(f) not in _GENERATED_LOCKFILES]

        if generated and not non_generated:
            # All conflicts are in generated lockfiles — auto-resolve by taking the stash version.
            resolve_failed = False
            for f in generated:
                co = run(["git", "checkout", stash_ref, "--", f], check=False)
                if co.returncode != 0:
                    resolve_failed = True
                    break
                add = run(["git", "add", f], check=False)
                if add.returncode != 0:
                    resolve_failed = True
                    break
            if resolve_failed:
                # Fall through to the manual-restore warning below.
                pass
            else:
                run(["git", "stash", "drop", stash_ref], check=False)
                names = ", ".join(os.path.basename(f) for f in generated)
                print(
                    f"Note: auto-resolved stash conflict in generated lockfile(s): {names}. "
                    "Stash restored.",
                    file=sys.stderr,
                )
                return

    print(
        f"Note: git stash pop {stash_ref} failed — "
        "run 'git stash list' and restore your changes manually.",
        file=sys.stderr,
    )
    if pop.stderr.strip():
        print(pop.stderr.strip(), file=sys.stderr)


def _warn_branch_auto_stash(task_id: int) -> None:
    """Warn about a leftover ``tusk-branch: auto-stash for TASK-<id>`` stash.

    Created by ``tusk branch <id>`` when the working tree was dirty at task-start
    time. The stash often contains pre-existing user WIP, so merge/abandon must
    never drop it silently. Silent when no matching entry exists.
    """
    stash_index: int | None = None
    for index, entry_task_id in iter_branch_auto_stashes(runner=run):
        if entry_task_id == task_id:
            stash_index = index
            break

    if stash_index is None:
        return

    stash_ref = f"stash@{{{stash_index}}}"
    print(
        f"Warning: preserved tusk branch auto-stash for TASK-{task_id} at {stash_ref}.\n"
        f"  Restore it with: git stash pop {stash_ref}\n"
        f"  Remove it with: git stash drop {stash_ref}",
        file=sys.stderr,
    )


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


def _recover_missing_task(db_path: str, task_id: int) -> bool:
    """Re-insert a minimal Done task record when the task row was lost to a WAL revert.

    Returns True on success, False on failure.
    """
    print(
        f"Warning: Task {task_id} not found in DB after merge — likely lost to a WAL revert. "
        "Re-inserting as Done to preserve merge integrity.",
        file=sys.stderr,
    )
    try:
        conn = get_connection(db_path)
        try:
            # task_type, priority, and complexity are intentionally omitted —
            # they are nullable in the schema and unknown after a WAL revert.
            conn.execute(
                "INSERT INTO tasks (id, summary, status, closed_reason, priority_score)"
                " VALUES (?, ?, 'Done', 'completed', 0)",
                (task_id, f"[Recovered after WAL revert] TASK-{task_id}"),
            )
            conn.commit()
        finally:
            conn.close()
        print(
            f"Recovered: Task {task_id} re-inserted as Done with closed_reason=completed.",
            file=sys.stderr,
        )
        return True
    except sqlite3.Error as e:
        print(
            f"Warning: Could not re-insert task {task_id} after WAL revert: {e}",
            file=sys.stderr,
        )
        return False


def _detect_id_gaps(db_path: str, task_id: int) -> list[int]:
    """Return task IDs missing in the range (max_id_below_task, task_id).

    After a WAL revert, tasks created between the last committed DB snapshot and
    task_id may be permanently lost. Queries the DB to find which IDs in that
    range are absent so the user can investigate.

    Returns an empty list if there are no gaps or if the DB cannot be queried.
    """
    try:
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(id) FROM tasks WHERE id < ?", (task_id,)
            ).fetchone()
            if row is None or row[0] is None:
                return []
            max_below = row[0]
            if max_below >= task_id - 1:
                return []  # no gap between max_below and task_id
            # All IDs in (max_below, task_id) are provably absent — max_below is
            # the largest existing ID below task_id, so no task can fill the gap.
            return list(range(max_below + 1, task_id))
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _close_completed_task(
    tusk_bin: str, task_id: int, db_path: str, session_was_closed: bool
) -> int:
    # Pass --force up front so task-done emits "Warning:" (not "Error:") for
    # criteria that legitimately lack a commit hash. The user has explicitly
    # chosen to ship the merge, so implicit --force on close is consistent with
    # that decision (issue #582; mirrors the auto-complete path's TASK-200 fix).
    print(f"Closing task {task_id}...", file=sys.stderr)
    result = _run_tusk_subcommand(
        tusk_bin, ["task-done", str(task_id), "--reason", "completed", "--force"]
    )
    if result.returncode != 0:
        if result.returncode == 2 and f"task {task_id} not found" in result.stderr.lower():
            # Task row missing — likely lost to a WAL revert that the checkpoint
            # above could not prevent (e.g. busy readers blocked full flush).
            # Re-insert as Done so the merge sequence can complete cleanly.
            recovered = _recover_missing_task(db_path, task_id)
            gap_ids = _detect_id_gaps(db_path, task_id)
            synthetic = {
                "task": {
                    "id": task_id,
                    "summary": f"[Recovered after WAL revert] TASK-{task_id}",
                    "status": "Done",
                    "closed_reason": "completed",
                },
                "sessions_closed": 1 if session_was_closed else 0,
                "unblocked_tasks": [],
                "wal_revert_recovery": recovered,
                "gap_task_ids": gap_ids,
            }
            if not recovered:
                print(
                    f"Warning: Task {task_id} could not be recovered. The branch has been "
                    "merged but the task record is permanently lost. Manually close it:\n"
                    f"  tusk task-insert \"[Recovered] TASK-{task_id}\" \"\" --priority Medium\n"
                    f"  tusk task-done <new_id> --reason completed --force",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Hint: Task {task_id} was recovered with placeholder metadata. "
                    "Update it with the correct values:\n"
                    f"  tusk task-update {task_id} --summary '...' --priority Medium "
                    f"--domain '...' --task-type '...' --complexity '...'",
                    file=sys.stderr,
                )
            if gap_ids:
                print(
                    f"Warning: {len(gap_ids)} task(s) between the last committed snapshot "
                    f"and TASK-{task_id} were lost in the WAL revert and cannot be "
                    f"recovered (these are separate from the task being merged): {gap_ids}\n"
                    "Investigate your git history or task notes to reconstruct them.",
                    file=sys.stderr,
                )
            print(dumps(synthetic))
            return 0
        print(f"Error: task-done failed:\n{result.stderr.strip()}", file=sys.stderr)
        return 2

    # Surface task-done's "Warning:" diagnostic (e.g. listing criteria that
    # were force-closed without a backing commit) so the audit trail still
    # reaches the user. Mirrors the auto-complete path's pattern.
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)

    # Forward the task-done JSON to stdout.
    try:
        task_done_result = json.loads(result.stdout)
    except json.JSONDecodeError:
        if result.stdout.strip():
            print(result.stdout.strip())
        return 0

    # tusk session-close already closed the session before task-done ran, so
    # task-done always sees 0 open sessions. Correct the counter here.
    if session_was_closed:
        task_done_result["sessions_closed"] = 1

    print(dumps(task_done_result))
    return 0


def _complete_no_checkout_fast_forward(
    *,
    branch_name: str,
    default_branch: str,
    task_id: int,
    session_id: int,
    tusk_bin: str,
    db_path: str,
    session_was_closed: bool,
    did_stash: bool,
    use_rebase: bool,
) -> int:
    print(
        f"Note: {default_branch} is checked out in another worktree; using "
        f"no-checkout fast-forward push from {branch_name} to {default_branch}.",
        file=sys.stderr,
    )
    if use_rebase:
        rebase_target = f"origin/{default_branch}"
        print(f"Rebasing {branch_name} onto {rebase_target}...", file=sys.stderr)
        fetch_result = run(["git", "fetch", "origin"], check=False)
        if fetch_result.returncode != 0:
            if _is_remote_unreachable(fetch_result.stderr):
                print(
                    f"Warning: could not reach origin — skipping --rebase before "
                    "no-checkout fast-forward push.\n"
                    f"  {fetch_result.stderr.strip()}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Error: git fetch origin failed before --rebase:\n"
                    f"{fetch_result.stderr.strip()}",
                    file=sys.stderr,
                )
                if did_stash:
                    _try_pop_stash(task_id)
                return 2
        else:
            co_result = run(["git", "checkout", branch_name], check=False)
            if co_result.returncode != 0:
                print(
                    f"Error: git checkout {branch_name} failed before --rebase:\n"
                    f"{co_result.stderr.strip()}",
                    file=sys.stderr,
                )
                if did_stash:
                    _try_pop_stash(task_id)
                return 2
            rebase_result = run(["git", "rebase", rebase_target], check=False)
            if rebase_result.returncode != 0:
                if rebase_result.stderr.strip():
                    print(rebase_result.stderr.strip(), file=sys.stderr)
                stash_note = ""
                if did_stash:
                    stash_note = (
                        f"\nNote: your pre-merge working-tree changes are saved in stash "
                        f"entry 'tusk-merge: auto-stash for TASK-{task_id}'. "
                        "Restore them with `git stash list` + `git stash pop <ref>` "
                        "after the rebase completes."
                    )
                print(
                    f"Error: git rebase {rebase_target} failed — conflicts must be resolved manually.\n"
                    f"You are on '{branch_name}' with the rebase in progress. To finish:\n"
                    "  1. Fix the conflicting files (git status lists them)\n"
                    "  2. git add <resolved files>\n"
                    "  3. git rebase --continue\n"
                    "  4. Repeat steps 1–3 until the rebase completes\n"
                    f"  5. Re-run: tusk merge {task_id}\n"
                    "To abort the rebase and return to the pre-rebase state:\n"
                    f"  git rebase --abort{stash_note}",
                    file=sys.stderr,
                )
                return 2
    else:
        fetch_result = run(["git", "fetch", "origin"], check=False)
        if fetch_result.returncode == 0:
            base_check = run(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    f"origin/{default_branch}",
                    branch_name,
                ],
                check=False,
            )
            if base_check.returncode != 0:
                print(
                    f"Error: origin/{default_branch} has commits not reachable from "
                    f"{branch_name}; refusing the no-checkout fast-forward push "
                    "before the remote rejects it.\n"
                    "To resolve:\n"
                    f"  tusk merge {task_id} --session {session_id} --rebase",
                    file=sys.stderr,
                )
                if did_stash:
                    _try_pop_stash(task_id)
                return 2
        elif _is_remote_unreachable(fetch_result.stderr):
            print(
                f"Warning: could not reach origin before no-checkout freshness "
                f"check — attempting push with local state.\n"
                f"  {fetch_result.stderr.strip()}",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: git fetch origin failed before no-checkout fast-forward push:\n"
                f"{fetch_result.stderr.strip()}",
                file=sys.stderr,
            )
            if did_stash:
                _try_pop_stash(task_id)
            return 2
    if _origin_already_contains(branch_name, default_branch):
        print(
            f"Note: origin/{default_branch} already contains {branch_name}'s "
            "tip — skipping no-checkout fast-forward push; the work has already "
            "shipped to origin (issue #774).",
            file=sys.stderr,
        )
    else:
        result = run(["git", "push", "origin", f"{branch_name}:{default_branch}"], check=False)
        if result.returncode != 0:
            print(
                f"Error: no-checkout fast-forward push failed:\n{result.stderr.strip()}\n"
                "The remote default branch was not updated. This usually means the "
                "feature branch is not a fast-forward of the remote default branch "
                "or the remote rejected the update.\n"
                "To resolve:\n"
                f"  git fetch origin && git rebase origin/{default_branch}\n"
                f"  tusk merge {task_id} --session {session_id}",
                file=sys.stderr,
            )
            if did_stash:
                _try_pop_stash(task_id)
            return 2
        if result.stdout.strip():
            print(result.stdout.strip(), file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        print(
            f"Note: pushed {branch_name} to origin/{default_branch}; leaving the local "
            "feature branch checked out because the default branch is locked by another "
            "worktree.",
            file=sys.stderr,
        )
    _delete_remote_feature_branch_if_tracking(branch_name)
    if not session_was_closed:
        checkpoint_wal(db_path)
        print(f"Closing session {session_id}...", file=sys.stderr)
        result = _run_tusk_subcommand(tusk_bin, ["session-close", str(session_id)])
        session_was_closed = result.returncode == 0
        if result.returncode != 0:
            if "already closed" in result.stderr:
                print(
                    f"Warning: session {session_id} is already closed — continuing.",
                    file=sys.stderr,
                )
            elif "No session found" in result.stderr:
                print(
                    f"Warning: session {session_id} not found in DB (may have been lost "
                    "to a WAL revert) — skipping session-close and continuing.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Error: session-close failed:\n{result.stderr.strip()}",
                    file=sys.stderr,
                )
                if did_stash:
                    _try_pop_stash(task_id)
                return 2
    if did_stash:
        _try_pop_stash(task_id)
    _warn_branch_auto_stash(task_id)
    # Post-merge cleanup for the no-checkout fast-forward push path (issue #765).
    # Before this fix the no-checkout path finalized session+task but left the
    # recorded task worktree, the task_workspaces row, and the local feature
    # branch behind — accumulating into 10+ stale worktrees across long-running
    # projects. The local ff-only path already did this cleanup; we replicate
    # it here so both paths converge on the same end-state.
    _cleanup_no_checkout_workspace(db_path, task_id, branch_name)
    return _close_completed_task(tusk_bin, task_id, db_path, session_was_closed)


def _cleanup_no_checkout_workspace(
    db_path: str, task_id: int, branch_name: str
) -> None:
    """Remove the recorded task worktree and delete the local feature branch.

    Called only on the success path of the no-checkout fast-forward push,
    where origin/<default> has just been updated to the feature branch's
    tip. Steps:
      1. Look up the recorded task workspace; if none, fall through to a
         best-effort branch delete and return.
      2. ``chdir`` out of the worktree (to the repo root) so ``git worktree
         remove`` can succeed — git refuses to remove the worktree it is
         currently operating in.
      3. ``git worktree remove`` via the existing ``_remove_recorded_task_worktree``
         helper, which also clears the ``task_workspaces`` row.
      4. ``git branch -D`` the local feature branch. ``-d``'s safety check
         compares against HEAD (whatever the repo root happens to be on),
         which after chdir is usually a branch that does NOT contain the
         feature branch's commits — even though origin/<default> does.
         Forcing the delete is safe here because the push has already
         succeeded; the commits are durable on origin/<default>.

    Any step that fails is surfaced as a Warning naming the remaining
    artifact and the reason, so the operator can resolve it manually
    rather than discovering a silent dangling state weeks later.
    """
    recorded = _recorded_task_workspace(db_path, task_id)
    if recorded is None:
        # No recorded workspace — legacy or hand-rolled feature branch.
        # Try a safe branch-delete only.
        result = run(["git", "branch", "-D", branch_name], check=False)
        if result.returncode != 0:
            print(
                f"Warning: git branch -D {branch_name} failed:\n"
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )
        return

    workspace_path = recorded["workspace_path"]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    try:
        os.chdir(repo_root)
    except OSError as exc:
        print(
            f"Warning: failed to chdir to repo root {repo_root} before "
            f"workspace cleanup: {exc}. Leaving worktree {workspace_path} "
            f"and feature branch {branch_name} in place. Run "
            f"`tusk task-worktree prune` and `git branch -D {branch_name}` "
            "manually.",
            file=sys.stderr,
        )
        return

    if not _remove_recorded_task_worktree(
        db_path, task_id, branch_name, workspace=recorded
    ):
        # _remove_recorded_task_worktree already printed the failure detail
        # (dirty worktree, etc.). Skip branch delete — git would refuse
        # anyway because the worktree still has the branch checked out.
        return

    result = run(["git", "branch", "-D", branch_name], check=False)
    if result.returncode != 0:
        print(
            f"Warning: git branch -D {branch_name} failed:\n"
            f"{result.stderr.strip()}\n"
            "The recorded worktree was removed but the local branch "
            "remained. Delete it manually with: "
            f"git branch -D {branch_name}",
            file=sys.stderr,
        )


def _recorded_task_workspace(db_path: str, task_id: int) -> sqlite3.Row | None:
    conn = get_connection(db_path)
    try:
        return conn.execute(
            """
            SELECT id, branch, workspace_path
            FROM task_workspaces
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    finally:
        conn.close()


def _forget_task_workspace(db_path: str, workspace_id: int) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM task_workspaces WHERE id = ?", (workspace_id,))
        conn.commit()
    finally:
        conn.close()


def _branch_exists(branch_name: str) -> bool:
    result = run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch_name}"],
        check=False,
    )
    return result.returncode == 0


def _origin_already_contains(ref_to_push: str, default_branch: str) -> bool:
    """Return True when ``origin/<default_branch>`` already contains every commit
    that ``ref_to_push`` would push to it — i.e. the push is a no-op.

    Used to skip pushes that would otherwise blow up against a pre-push hook
    after the operator manually pushed with ``--no-verify`` and fast-forwarded
    local default to match (issue #774). On any rev-list failure (e.g. no
    ``origin/<default>`` ref locally) the function returns False so the caller
    falls through to the normal push and surfaces the real error.
    """
    result = run(
        [
            "git",
            "rev-list",
            f"origin/{default_branch}..{ref_to_push}",
            "--count",
        ],
        check=False,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "0"


def _branch_has_task_commits(
    branch_name: str, task_id: int, default_branch: str
) -> bool:
    """Return True when ``branch_name`` has any ``[TASK-<task_id>]`` commits ahead of ``default_branch``.

    Implemented via ``mod.run`` (rather than ``find_task_commits``) so unit
    tests that patch this module's ``run`` can stub the result without
    reaching real git.
    """
    result = run(
        [
            "git",
            "log",
            f"{default_branch}..{branch_name}",
            "--format=%H",
            task_grep_arg(task_id),
        ],
        check=False,
    )
    if result.returncode != 0:
        return False
    return any(line.strip() for line in result.stdout.splitlines())


def _delete_remote_feature_branch_if_tracking(branch_name: str) -> None:
    """Delete origin/<branch_name> when the local branch tracks that exact ref."""
    remote = run(
        ["git", "config", "--get", f"branch.{branch_name}.remote"],
        check=False,
    )
    if remote.returncode != 0 or remote.stdout.strip() != "origin":
        return

    merge_ref = run(
        ["git", "config", "--get", f"branch.{branch_name}.merge"],
        check=False,
    )
    if merge_ref.returncode != 0:
        return
    if merge_ref.stdout.strip() != f"refs/heads/{branch_name}":
        return

    result = run(["git", "push", "origin", "--delete", branch_name], check=False)
    if result.returncode == 0:
        print(
            f"Deleted remote feature branch origin/{branch_name}.",
            file=sys.stderr,
        )
    else:
        print(
            f"Warning: git push origin --delete {branch_name} failed:\n"
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )


def _remove_recorded_task_worktree(
    db_path: str,
    task_id: int,
    branch_name: str,
    retry_command: str | None = None,
    workspace: sqlite3.Row | None | object = _WORKSPACE_NOT_PROVIDED,
) -> bool:
    """Remove the recorded task-owned worktree before deleting its branch.

    `git branch -d/-D` refuses to delete a branch that is checked out in any
    linked worktree. Removing the task-owned worktree first also gives dirty
    worktrees a natural safety gate: plain `git worktree remove` fails until the
    operator cleans/stashes the files or explicitly force-removes that worktree.
    """
    if workspace is _WORKSPACE_NOT_PROVIDED:
        workspace = _recorded_task_workspace(db_path, task_id)
    if workspace is None:
        return True
    if retry_command is None:
        retry_command = f"tusk merge {task_id}"
    if workspace["branch"] != branch_name:
        print(
            f"Warning: recorded task workspace branch {workspace['branch']} does "
            f"not match selected branch {branch_name}; leaving it untouched.",
            file=sys.stderr,
        )
        return True

    workspace_path = workspace["workspace_path"]
    if os.path.exists(workspace_path):
        result = run(["git", "worktree", "remove", workspace_path], check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            print(
                f"Error: git worktree remove {workspace_path} failed:\n{detail}\n"
                "Clean or stash that task worktree, then re-run this command. "
                "If you intentionally want to discard its local files, run:\n"
                f"  git worktree remove --force {workspace_path}\n"
                f"  {retry_command}",
                file=sys.stderr,
            )
            return False

    _forget_task_workspace(db_path, workspace["id"])
    return True


def find_task_branch(task_id: int) -> tuple[str | None, str | None, bool]:
    """Return (branch_name, error_message, pre_merged).

    pre_merged is True when no feature branch exists but the user is already on
    the default branch — indicating the branch was previously merged and deleted.
    When pre_merged is True, branch_name and error_message are both None.
    """
    primary_pattern = f"feature/TASK-{task_id}-*"
    fallback_pattern = f"worktree-TASK-{task_id}-*"

    result = run(["git", "branch", "--list", primary_pattern], check=False)
    if result.returncode != 0:
        return None, f"git branch --list {primary_pattern} failed: {result.stderr.strip()}", False

    branches = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("* ", "+ ")):
            stripped = stripped[2:]
        if stripped:
            branches.append(stripped)

    if len(branches) == 0:
        result = run(["git", "branch", "--list", fallback_pattern], check=False)
        if result.returncode != 0:
            return None, (
                f"git branch --list {fallback_pattern} failed: {result.stderr.strip()}"
            ), False

        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith(("* ", "+ ")):
                stripped = stripped[2:]
            if stripped:
                branches.append(stripped)

    if len(branches) == 0:
        # Check if user is on the default branch (branch was previously merged)
        current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
        default = detect_default_branch()
        if current.returncode == 0 and current.stdout.strip() == default:
            return None, None, True  # pre-merged: auto-complete path
        return None, (
            f"No branch found matching {primary_pattern} or {fallback_pattern}"
        ), False
    if len(branches) > 1:
        # First filter by [TASK-<id>] commit presence: a branch matching
        # feature/TASK-<id>-* that carries no task commits ahead of the default
        # branch is almost certainly a stale slug from an abandoned session, and
        # ranking such a branch above one that actually has the user's work by
        # tip-commit recency silently merges the wrong branch (issue #763).
        default = detect_default_branch()
        with_commits = [
            b for b in branches if _branch_has_task_commits(b, task_id, default)
        ]

        if len(with_commits) > 1:
            names = ", ".join(with_commits)
            return None, (
                f"Multiple branches found for TASK-{task_id} each containing "
                f"[TASK-{task_id}] commits ahead of {default}: {names}. "
                "Delete or merge all but one before running tusk merge."
            ), False

        if len(with_commits) == 1:
            selected = with_commits[0]
            others = [b for b in branches if b != selected]
            print(
                f"Note: Multiple branches found for TASK-{task_id} "
                f"({', '.join(branches)}). "
                f"Selecting branch with [TASK-{task_id}] commits: {selected}. "
                f"Branch(es) without task commits not removed: {', '.join(others)}.",
                file=sys.stderr,
            )
            return selected, None, False

        # No branch carries task commits — fall back to the tip-commit-recency
        # tiebreaker so behavior matches pre-#763 history when every candidate
        # is equally empty.
        timestamps = {}
        for b in branches:
            ts_result = run(
                ["git", "log", "-1", "--format=%ct", b], check=False
            )
            if ts_result.returncode == 0 and ts_result.stdout.strip().isdigit():
                timestamps[b] = int(ts_result.stdout.strip())
            else:
                timestamps[b] = 0

        max_ts = max(timestamps.values())
        most_recent = [b for b, ts in timestamps.items() if ts == max_ts]

        if len(most_recent) == 1:
            selected = most_recent[0]
            others = [b for b in branches if b != selected]
            print(
                f"Note: Multiple branches found for TASK-{task_id} "
                f"({', '.join(branches)}). "
                f"Selecting most-recent-commit branch: {selected}. "
                f"Stale branch(es) not removed: {', '.join(others)}.",
                file=sys.stderr,
            )
            return selected, None, False
        else:
            # Exact tie — prompt the user to choose.
            names = ", ".join(branches)
            return None, (
                f"Multiple branches found for TASK-{task_id} with equal recency: {names}. "
                "Delete all but one before running tusk merge."
            ), False
    return branches[0], None, False


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
            branch_check, _, _ = find_task_branch(task_id)
            if branch_check:
                print(
                    f"Warning: No session found for task {task_id} — tasks.db may have been "
                    "reverted by a git stash or checkout. Creating a synthetic session to "
                    "allow merge to proceed.\n"
                    "Tip: add the tusk database to your .gitignore (run `tusk path` to find "
                    "the exact path) to prevent this in future.",
                    file=sys.stderr,
                )
                result = _run_tusk_subcommand(tusk_bin, ["task-start", str(task_id), "--force"])
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
            "Usage: tusk merge <task_id> [--session <session_id>] [--pr] [--pr-number N] [--rebase]",
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

    # Locate the tusk binary. Prefer the primary install (resolved from db_path's
    # repo root) over a __file__-derived sibling so post-cleanup subprocess calls
    # remain valid when this script runs inside a task worktree that gets removed
    # mid-flow on the no-checkout fast-forward path (issue #834).
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tusk_bin = _resolve_stable_tusk_bin(_db_path, os.path.join(script_dir, "tusk"))

    # Parse remaining flags
    remaining = argv[3:]
    session_id = None
    use_pr = False
    pr_number = None
    use_rebase = False

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
        elif remaining[i] == "--rebase":
            use_rebase = True
            i += 1
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

    if use_pr and use_rebase:
        print(
            "Warning: --rebase is ignored in PR mode (squash merge via gh pr merge does not rebase).",
            file=sys.stderr,
        )

    # Preflight checks — abort before touching session or task state
    # Step 1a: Detect feature branch. Prefer the task-owned workspace record
    # when present; it is the explicit ownership edge for this task and avoids
    # selecting a stale or unrelated feature/TASK-N-* branch by timestamp.
    # The recorded pointer is only honored when its branch (a) exists locally
    # AND (b) contains [TASK-<id>] commits ahead of the default branch.
    # Otherwise it is treated as stale (abandoned session leftover) and the
    # commit-pattern scan in find_task_branch picks the real branch — without
    # this validation, a stale empty branch silently wins over the user's real
    # work (issue #763).
    recorded_workspace = _recorded_task_workspace(_db_path, task_id)
    if recorded_workspace is not None:
        candidate_branch = recorded_workspace["branch"]
        candidate_path = recorded_workspace["workspace_path"]
        default_branch_probe = detect_default_branch()
        branch_exists = _branch_exists(candidate_branch)
        has_task_commits = branch_exists and _branch_has_task_commits(
            candidate_branch, task_id, default_branch_probe
        )
        path_exists = os.path.exists(candidate_path)
        if branch_exists and has_task_commits:
            branch_name = candidate_branch
            err = None
            pre_merged = False
            print(
                f"Found recorded task workspace branch: {branch_name}",
                file=sys.stderr,
            )
            # When the recorded workspace points at a real on-disk worktree and
            # the operator launched tusk merge from a different CWD that is NOT
            # on the default branch, switch into the recorded workspace so the
            # rebase/checkout/push operations land on the feature branch's
            # index instead of a possibly-dirty primary repo's index. The
            # canonical failure mode: primary repo is on some other feature
            # branch with stray uncommitted files (session-start dirty state
            # like .claude/settings.json), the feature branch under merge
            # lives in a separate worktree, and `tusk merge --rebase` blows
            # up with a misleading "cannot rebase: You have unstaged changes"
            # that names the primary's files (issue #764). Skip the chdir
            # when CWD is already on the default branch — the existing
            # ff-only path expects to run `git merge --ff-only feature` from
            # whichever worktree has the default branch checked out.
            if path_exists:
                try:
                    current_cwd_real = os.path.realpath(os.getcwd())
                except OSError:
                    current_cwd_real = ""
                workspace_real = os.path.realpath(candidate_path)
                if current_cwd_real != workspace_real:
                    current_branch_result = run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        check=False,
                    )
                    current_branch = (
                        current_branch_result.stdout.strip()
                        if current_branch_result.returncode == 0
                        else ""
                    )
                    if current_branch != default_branch_probe:
                        os.chdir(candidate_path)
                        print(
                            f"Note: switched CWD to recorded task workspace "
                            f"{candidate_path} so rebase/push/branch-delete "
                            "operate on the feature branch's worktree, not the "
                            "primary repo.",
                            file=sys.stderr,
                        )
        else:
            reasons = []
            if not branch_exists:
                reasons.append(f"branch '{candidate_branch}' does not exist")
            elif not has_task_commits:
                reasons.append(
                    f"branch '{candidate_branch}' has no [TASK-{task_id}] "
                    f"commits ahead of {default_branch_probe}"
                )
            if not path_exists:
                reasons.append(
                    f"workspace path '{candidate_path}' is missing on disk"
                )
            joined = "; ".join(reasons)
            print(
                f"Warning: recorded task workspace is stale ({joined}); "
                "falling back to commit-pattern scan. Run `tusk task-worktree "
                "prune` to clean up the stale registry row.",
                file=sys.stderr,
            )
            branch_name, err, pre_merged = find_task_branch(task_id)
    else:
        branch_name, err, pre_merged = find_task_branch(task_id)

    if pre_merged:
        # Fast-path: feature branch was already merged and deleted; user is on
        # the default branch. Skip the normal merge and auto-complete finalization.
        default_branch = detect_default_branch()
        print(
            f"Note: TASK-{task_id} — no feature branch found; already on '{default_branch}'.\n"
            "Branch was previously merged. Auto-completing finalization...",
            file=sys.stderr,
        )
        checkpoint_wal(_db_path)
        print(f"Closing session {session_id}...", file=sys.stderr)
        result = _run_tusk_subcommand(tusk_bin, ["session-close", str(session_id)])
        session_was_closed = result.returncode == 0
        if result.returncode != 0:
            if "already closed" in result.stderr or "No session found" in result.stderr:
                print(f"Warning: {result.stderr.strip()}", file=sys.stderr)
            else:
                print(f"Error: session-close failed:\n{result.stderr.strip()}", file=sys.stderr)
                return 2
        # Push the default branch — may already be up to date if merged via PR
        if _has_remote():
            push = run(["git", "push", "origin", default_branch], check=False)
            if push.returncode != 0:
                print(
                    f"Warning: git push origin {default_branch} failed — "
                    f"branch may already be pushed:\n{push.stderr.strip()}",
                    file=sys.stderr,
                )
        else:
            print(
                "Warning: no git remote 'origin' configured — skipping push.",
                file=sys.stderr,
            )
        print(f"Closing task {task_id}...", file=sys.stderr)
        # Auto-complete path implicitly grants --force: the feature branch was
        # previously merged so the criteria-without-commit-hash check, run
        # without --force, would print a misleading "Error:" before the call
        # site retried with --force. Pass --force up front so task-done emits
        # "Warning:" instead — diagnostic preserved, no contradiction.
        result = _run_tusk_subcommand(
            tusk_bin, ["task-done", str(task_id), "--reason", "completed", "--force"]
        )
        if result.returncode != 0:
            print(f"Error: task-done failed:\n{result.stderr.strip()}", file=sys.stderr)
            return 2
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        try:
            task_done_result = json.loads(result.stdout)
        except json.JSONDecodeError:
            if result.stdout.strip():
                print(result.stdout.strip())
            return 0
        if session_was_closed:
            task_done_result["sessions_closed"] = 1
        print(dumps(task_done_result))
        return 0

    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    print(f"Found branch: {branch_name}", file=sys.stderr)

    default_branch = None
    has_origin = None
    if not use_pr:
        default_branch = detect_default_branch()
        has_origin = _has_remote()
        locked_default_path = _worktree_path_for_branch(default_branch)
        if locked_default_path:
            print(
                f"Merging {branch_name} into {default_branch} (ff-only)...",
                file=sys.stderr,
            )
            if not has_origin:
                print(
                    f"Error: git checkout {default_branch} would fail because the branch "
                    f"is checked out in another worktree at '{locked_default_path}', and "
                    "no git remote 'origin' is configured for a no-checkout "
                    "fast-forward push.",
                    file=sys.stderr,
                )
                return 2
            return _complete_no_checkout_fast_forward(
                branch_name=branch_name,
                default_branch=default_branch,
                task_id=task_id,
                session_id=session_id,
                tusk_bin=tusk_bin,
                db_path=_db_path,
                session_was_closed=False,
                did_stash=False,
                use_rebase=use_rebase,
            )

    # Step 1b (local mode only): Auto-stash if working tree is dirty.
    # Only tracked modified/staged files are stashed; untracked files ("??")
    # are not uncommitted changes and carry over automatically.
    # tasks.db is gitignored and therefore excluded from git stash automatically.
    # We do NOT pass a pathspec to git stash push — scoped pathspecs can fail
    # with "pathspec did not match any file(s) known to git" for root-level
    # files even when git diff --name-only reports them as modified (issue #339).
    did_stash = False
    if not use_pr:
        unstaged = run(["git", "diff", "--name-only"], check=False)
        staged = run(["git", "diff", "--cached", "--name-only"], check=False)
        if unstaged.returncode != 0 or staged.returncode != 0:
            err = unstaged.stderr.strip() or staged.stderr.strip()
            print(f"Error: git diff failed:\n{err}", file=sys.stderr)
            return 1
        # Filter is used only to gate whether a stash is attempted at all —
        # tasks.db is gitignored so git stash excludes it automatically.
        dirty_files = list(dict.fromkeys(
            f
            for f in unstaged.stdout.splitlines() + staged.stdout.splitlines()
            if f and not f.startswith("tusk/tasks.db")
        ))
        if dirty_files:
            print("Stashing uncommitted changes before merging...", file=sys.stderr)
            stash = run(
                ["git", "stash", "push", "-m", f"tusk-merge: auto-stash for TASK-{task_id}"],
                check=False,
            )
            if stash.returncode != 0:
                print(f"Error: git stash failed:\n{stash.stderr.strip()}", file=sys.stderr)
                return 1
            did_stash = "No local changes to save" not in stash.stdout

    if not use_pr:
        if default_branch is None:
            default_branch = detect_default_branch()
        if has_origin is None:
            has_origin = _has_remote()
        locked_default_path = _worktree_path_for_branch(default_branch)
        if locked_default_path:
            print(
                f"Merging {branch_name} into {default_branch} (ff-only)...",
                file=sys.stderr,
            )
            if not has_origin:
                print(
                    f"Error: git checkout {default_branch} would fail because the branch "
                    f"is checked out in another worktree at '{locked_default_path}', and "
                    "no git remote 'origin' is configured for a no-checkout "
                    "fast-forward push.",
                    file=sys.stderr,
                )
                if did_stash:
                    _try_pop_stash(task_id)
                return 2
            return _complete_no_checkout_fast_forward(
                branch_name=branch_name,
                default_branch=default_branch,
                task_id=task_id,
                session_id=session_id,
                tusk_bin=tusk_bin,
                db_path=_db_path,
                session_was_closed=False,
                did_stash=did_stash,
                use_rebase=use_rebase,
            )

    # Step 2: Close the session (captures git diff stats while on feature branch)
    #
    # Checkpoint the WAL first so that any uncommitted writes (e.g. from tusk task-start)
    # are flushed to the main db file before session-close reads the session row.
    # Without this, a git stash or branch switch that reverts tasks.db to a pre-WAL
    # snapshot can silently drop the session row, causing "No session found" below.
    checkpoint_wal(_db_path)

    print(f"Closing session {session_id}...", file=sys.stderr)
    result = _run_tusk_subcommand(tusk_bin, ["session-close", str(session_id)])
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
            if did_stash:
                _try_pop_stash(task_id)
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
        if default_branch is None:
            default_branch = detect_default_branch()
        print(f"Merging {branch_name} into {default_branch} (ff-only)...", file=sys.stderr)
        if has_origin is None:
            has_origin = _has_remote()

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

        result = _run_with_index_lock_retry(
            ["git", "checkout", default_branch], f"git checkout {default_branch}"
        )
        if result.returncode != 0:
            for src, dst in moved:
                os.rename(dst, src)
            if _is_default_branch_locked_by_worktree(result.stderr, default_branch):
                if not has_origin:
                    print(
                        f"Error: git checkout {default_branch} failed because the branch "
                        "is checked out in another worktree, and no git remote 'origin' "
                        "is configured for a no-checkout fast-forward push.\n"
                        f"{result.stderr.strip()}",
                        file=sys.stderr,
                    )
                    if did_stash:
                        _try_pop_stash(task_id)
                    return 2
                return _complete_no_checkout_fast_forward(
                    branch_name=branch_name,
                    default_branch=default_branch,
                    task_id=task_id,
                    session_id=session_id,
                    tusk_bin=tusk_bin,
                    db_path=_db_path,
                    session_was_closed=session_was_closed,
                    did_stash=did_stash,
                    use_rebase=use_rebase,
                )
            print(
                f"Error: git checkout {default_branch} failed:\n{result.stderr.strip()}",
                file=sys.stderr,
            )
            if did_stash:
                _try_pop_stash(task_id)
            return 2

        # Restore db files after successful checkout
        for src, dst in moved:
            os.rename(dst, src)

        # Step 4: Pull latest (skip when no remote is configured or unreachable)
        if has_origin:
            result = run(["git", "-c", "pull.rebase=false", "pull", "origin", default_branch], check=False)
            if result.returncode != 0:
                if _is_remote_unreachable(result.stderr):
                    print(
                        f"Warning: could not reach origin — skipping pull. "
                        f"Merging from local '{default_branch}'.\n  {result.stderr.strip()}",
                        file=sys.stderr,
                    )
                else:
                    print(f"Error: git pull failed:\n{result.stderr.strip()}", file=sys.stderr)
                    # Restore feature branch so user can investigate
                    run(["git", "checkout", branch_name], check=False)
                    if did_stash:
                        _try_pop_stash(task_id)
                    return 2
        else:
            print(
                "Warning: no git remote 'origin' configured — skipping pull. "
                "Merging from local state.",
                file=sys.stderr,
            )

        # Guard (issue #607): refuse to silently push unrelated commits that snuck
        # onto local <default_branch> from a prior session. Runs before any path-
        # specific logic so it covers rebase, ff-only, and task_on_default merges
        # uniformly — all three reach the same `git push origin <default>` step.
        if has_origin:
            unpushed = _local_default_unpushed_commits(default_branch)
            if unpushed:
                if not _confirm_proceed_with_unpushed(unpushed, default_branch, task_id):
                    run(["git", "checkout", branch_name], check=False)
                    if did_stash:
                        _try_pop_stash(task_id)
                    return 2

        # Detect zero-new-commits case first: feature branch has no exclusive commits
        # over the default branch. Legitimate for triage-only tasks whose deliverable
        # was a follow-up task creation (or any task that closed without code changes).
        # Without this check, the branch falls into the task_on_default path below with
        # a misleading "feature branch is diverged" message — the branch isn't diverged,
        # it's identical to default.
        _count_check = run(
            ["git", "rev-list", "--count", f"{default_branch}..{branch_name}"],
            check=False,
        )
        no_new_commits = (
            _count_check.returncode == 0 and _count_check.stdout.strip() == "0"
        )
        if no_new_commits:
            print(
                f"Note: TASK-{task_id} has no new commits on the feature branch — "
                "closing without merge.",
                file=sys.stderr,
            )
            task_on_default = True
        else:
            # Detect if the task commit was already applied directly on the default branch
            # (e.g. a rebase conflict resolved by re-applying the fix on main). When true,
            # the feature branch is diverged and cannot be fast-forwarded — skip the
            # rebase/ff-merge steps and proceed directly to push + cleanup.
            #
            # Scoped to commits reachable from the feature branch but NOT from the default
            # branch (<branch> --not <default>). This prevents false-positives when task IDs
            # are recycled after a DB reset: an old [TASK-N] commit on the default branch
            # would be matched by a naïve `git log <default> --grep` but is irrelevant to the
            # current feature branch. If the feature branch has no exclusive [TASK-N] commits
            # (empty result), the task's changes must already be on the default branch.
            _log_check = run(
                ["git", "log", branch_name, "--not", default_branch, "--oneline",
                 task_grep_arg(task_id)],
                check=False,
            )
            task_on_default = (
                _log_check.returncode == 0 and not bool(_log_check.stdout.strip())
            )
            if task_on_default:
                print(
                    f"Note: TASK-{task_id} commit already on {default_branch} — "
                    "feature branch is diverged. Skipping ff-only merge.",
                    file=sys.stderr,
                )

        # Secondary check: use git cherry to detect commits that were cherry-picked
        # onto the default branch (same patch content, different hash). The log-scoped
        # check above finds the feature branch's own [TASK-N] commit and sets
        # task_on_default=False, but if that commit was cherry-picked to default the
        # ff-only merge will fail. git cherry compares by patch ID, so cherry-picked
        # equivalents appear as '-' lines. If every exclusive commit on the feature
        # branch is already applied (all '-', no '+'), the branch is safe to discard.
        if not task_on_default:
            _cherry_check = run(
                ["git", "cherry", default_branch, branch_name],
                check=False,
            )
            if _cherry_check.returncode != 0:
                print(
                    f"Warning: git cherry {default_branch} {branch_name} failed — "
                    "cherry-pick detection skipped. The ff-only merge will proceed "
                    "and may fail if the branch was cherry-picked.",
                    file=sys.stderr,
                )
            if _cherry_check.returncode == 0:
                _cherry_lines = [
                    line for line in _cherry_check.stdout.splitlines() if line.strip()
                ]
                if _cherry_lines and not any(
                    line.startswith("+ ") for line in _cherry_lines
                ):
                    task_on_default = True
                    print(
                        f"Note: TASK-{task_id} — all feature branch commits already "
                        f"applied to {default_branch} via cherry-pick. "
                        "Skipping ff-only merge.",
                        file=sys.stderr,
                    )

        # Prefix-collision file-overlap heuristic (issue #656).
        # Both detection paths above (branch-scoped [TASK-N] log-check at lines
        # 1006-1013, and the cherry-pick patch-equivalence check at lines 1031-1056)
        # can falsely set task_on_default = True when the feature branch's commits
        # don't follow the [TASK-N] tagging convention or when another task's commit
        # was tagged with this task's prefix by mistake. Once that flag is True the
        # downstream path skips the ff-only merge AND force-deletes the feature
        # branch — recovery requires git reflog. Validate it via the same heuristic
        # tusk-check-deliverables.py uses to downgrade `merged_not_closed` to
        # `merged_not_closed_low_confidence` (issue #606): compare the matched
        # commits' file diff against the task's referenced paths. Skipped on the
        # no_new_commits branch — there's nothing to fast-forward there, so the
        # heuristic doesn't apply.
        if task_on_default and not no_new_commits:
            _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(_db_path)))
            _matched_default_commits = find_task_commits(
                task_id, _repo_root, [default_branch]
            )
            if not _matched_default_commits:
                # No [TASK-N] commits on default to compare. The log-check inferred
                # 'on default' purely from the absence of branch-side [TASK-N]
                # commits — not supportable when the feature branch has commits
                # under a different tag (e.g. a cherry-pick from another repo).
                # Reset and let the ff-merge / rebase paths below decide.
                print(
                    f"Note: TASK-{task_id} — log-check inferred 'commit on "
                    f"{default_branch}' but no [TASK-{task_id}] commits found there; "
                    f"feature branch has new commits ahead of {default_branch}. "
                    "Proceeding with ff-only merge to avoid orphaning unmerged work "
                    "(issue #656).",
                    file=sys.stderr,
                )
                task_on_default = False
            else:
                _matched_files = commit_changed_files(
                    _matched_default_commits, _repo_root
                )
                _conn = get_connection(_db_path)
                try:
                    _task_paths = set(task_referenced_paths(task_id, _conn))
                finally:
                    _conn.close()
                _sha_list = " ".join(s[:7] for s in _matched_default_commits)
                if _task_paths and not (_task_paths & _matched_files):
                    print(
                        f"Note: TASK-{task_id} — matched [TASK-{task_id}] commits "
                        f"on {default_branch} ({_sha_list}) don't overlap with this "
                        "task's referenced files; treating as prefix-match false "
                        "positive and proceeding with ff-only merge (issue #656).",
                        file=sys.stderr,
                    )
                    task_on_default = False
                else:
                    # High-confidence: log the matched SHAs so operators can spot
                    # mismatches without git log archaeology (issue #656).
                    print(
                        f"Note: TASK-{task_id} — matched [TASK-{task_id}] commits "
                        f"on {default_branch}: {_sha_list}",
                        file=sys.stderr,
                    )

        # Step 4 (optional --rebase): rebase feature branch onto default before ff-merge
        if not task_on_default and use_rebase:
            rebase_target = default_branch
            if has_origin:
                fetch_result = run(["git", "fetch", "origin"], check=False)
                if fetch_result.returncode == 0:
                    rebase_target = f"origin/{default_branch}"
                elif _is_remote_unreachable(fetch_result.stderr):
                    print(
                        f"Warning: could not reach origin — rebasing onto local "
                        f"'{default_branch}'.\n  {fetch_result.stderr.strip()}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"Error: git fetch origin failed before --rebase:\n"
                        f"{fetch_result.stderr.strip()}",
                        file=sys.stderr,
                    )
                    run(["git", "checkout", branch_name], check=False)
                    if did_stash:
                        _try_pop_stash(task_id)
                    return 2
            print(f"Rebasing {branch_name} onto {rebase_target}...", file=sys.stderr)
            # Switch to feature branch — move db files aside first (same pattern as above)
            for src, dst in zip(db_siblings, db_tmp):
                if os.path.exists(src):
                    os.rename(src, dst)
            co_result = run(["git", "checkout", branch_name], check=False)
            for src, dst in zip(db_siblings, db_tmp):
                if os.path.exists(dst):
                    os.rename(dst, src)
            if co_result.returncode != 0:
                print(
                    f"Error: git checkout {branch_name} failed:\n{co_result.stderr.strip()}",
                    file=sys.stderr,
                )
                run(["git", "checkout", default_branch], check=False)
                if did_stash:
                    _try_pop_stash(task_id)
                return 2

            rebase_result = run(["git", "rebase", rebase_target], check=False)
            if rebase_result.returncode != 0:
                if rebase_result.stderr.strip():
                    print(rebase_result.stderr.strip(), file=sys.stderr)
                stash_note = ""
                if did_stash:
                    stash_note = (
                        f"\nNote: your pre-merge working-tree changes are saved in stash "
                        f"entry 'tusk-merge: auto-stash for TASK-{task_id}'. "
                        "Restore them with `git stash list` + `git stash pop <ref>` "
                        "after the rebase completes."
                    )
                print(
                    f"Error: git rebase {rebase_target} failed — conflicts must be resolved manually.\n"
                    f"You are on '{branch_name}' with the rebase in progress. To finish:\n"
                    "  1. Fix the conflicting files (git status lists them)\n"
                    "  2. git add <resolved files>\n"
                    "  3. git rebase --continue\n"
                    "  4. Repeat steps 1–3 until the rebase completes\n"
                    f"  5. Re-run: tusk merge {task_id}\n"
                    "To abort the rebase and return to the pre-rebase state:\n"
                    f"  git rebase --abort{stash_note}",
                    file=sys.stderr,
                )
                return 2

            # Rebase succeeded — switch back to default branch for ff-only merge
            for src, dst in zip(db_siblings, db_tmp):
                if os.path.exists(src):
                    os.rename(src, dst)
            co_back = run(["git", "checkout", default_branch], check=False)
            for src, dst in zip(db_siblings, db_tmp):
                if os.path.exists(dst):
                    os.rename(dst, src)
            if co_back.returncode != 0:
                print(
                    f"Error: git checkout {default_branch} failed after rebase:\n{co_back.stderr.strip()}",
                    file=sys.stderr,
                )
                if did_stash:
                    _try_pop_stash(task_id)
                return 2

        # Step 4 (cont): Fast-forward merge (skipped when task commit already on default)
        if not task_on_default:
            result = _run_with_index_lock_retry(
                ["git", "merge", "--ff-only", branch_name],
                f"git merge --ff-only {branch_name}",
            )
            if result.returncode != 0:
                print(
                    f"Error: git merge --ff-only {branch_name} failed:\n{result.stderr.strip()}\n"
                    "The feature branch cannot be fast-forward merged. Run one of:\n"
                    f"  git rebase origin/{default_branch}  # rebase manually, then re-run: tusk merge {task_id}\n"
                    f"  tusk merge {task_id} --rebase        # auto-rebase before merging\n"
                    f"  tusk merge {task_id} --pr --pr-number <N>  # squash merge via PR",
                    file=sys.stderr,
                )
                # Restore feature branch so user can investigate
                run(["git", "checkout", branch_name], check=False)
                if did_stash:
                    _try_pop_stash(task_id)
                return 2

        # Step 5: Push (skip when no remote is configured or unreachable)
        if has_origin:
            if _origin_already_contains(default_branch, default_branch):
                print(
                    f"Note: origin/{default_branch} already contains "
                    f"{default_branch}'s commits — skipping push; the work has "
                    "already shipped to origin (issue #774).",
                    file=sys.stderr,
                )
                result = None
            else:
                result = run(["git", "push", "origin", default_branch], check=False)
            if result is not None and result.returncode != 0:
                if _is_remote_unreachable(result.stderr):
                    print(
                        f"Warning: could not reach origin — skipping push. "
                        f"The merge is complete locally on '{default_branch}'.\n  {result.stderr.strip()}\n"
                        f"  Retry later: git push origin {default_branch}",
                        file=sys.stderr,
                    )
                elif task_on_default:
                    print(
                        f"Error: git push failed:\n{result.stderr.strip()}\n"
                        f"  Retry: git push origin {default_branch} && tusk merge {task_id} --session {session_id}",
                        file=sys.stderr,
                    )
                    if did_stash:
                        _try_pop_stash(task_id)
                    return 2
                else:
                    if use_rebase:
                        error_title = "Error: git push failed after --rebase:"
                        retry = (
                            f"  Retry: git fetch origin && git rebase origin/{default_branch} && "
                            f"git push origin {default_branch} && tusk merge {task_id} --session {session_id}"
                        )
                        context = (
                            "The branch was rebased for --rebase and merged locally, "
                            "but origin still rejected the push. The remote default "
                            "branch may have advanced after the rebase."
                        )
                    else:
                        error_title = "Error: git push failed:"
                        retry = (
                            f"  Retry: git push origin {default_branch} && tusk merge {task_id} --session {session_id}"
                        )
                        context = "The branch has been merged locally but not pushed."
                    print(
                        f"{error_title}\n{result.stderr.strip()}\n"
                        f"{context}\n"
                        f"{retry}\n"
                        f"  Undo:  git reset --hard HEAD~1 && git checkout {branch_name}",
                        file=sys.stderr,
                    )
                    if did_stash:
                        # Restore feature branch before popping stash so the user's
                        # uncommitted changes land back on the feature branch, not on
                        # the default branch where the unmerged commit lives.
                        run(["git", "checkout", branch_name], check=False)
                        _try_pop_stash(task_id)
                    return 2
        else:
            print(
                "Warning: no git remote 'origin' configured — skipping push.",
                file=sys.stderr,
            )
        if has_origin:
            _delete_remote_feature_branch_if_tracking(branch_name)

        # Step 6: Delete feature branch
        if not _remove_recorded_task_worktree(
            _db_path, task_id, branch_name, workspace=recorded_workspace
        ):
            if did_stash:
                _try_pop_stash(task_id)
            return 2

        # Use -D (force) when the branch was not merged via git merge (task_on_default path).
        branch_delete_flag = "-D" if task_on_default else "-d"
        result = run(["git", "branch", branch_delete_flag, branch_name], check=False)
        if result.returncode != 0:
            # Non-fatal: branch is already gone or merge state mismatch, warn and continue
            print(
                f"Warning: git branch {branch_delete_flag} {branch_name} failed:\n{result.stderr.strip()}",
                file=sys.stderr,
            )

        if did_stash:
            _try_pop_stash(task_id)

    # Warn about any leftover `tusk-branch: auto-stash for TASK-<id>` entry created
    # during a prior `tusk branch <id>` invocation when the working tree was
    # dirty. It can contain pre-existing user WIP, so preserve it and surface
    # explicit manual restore/drop commands.
    _warn_branch_auto_stash(task_id)

    return _close_completed_task(tusk_bin, task_id, _db_path, session_was_closed)


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk merge <task_id> [--session <session_id>] [--pr --pr-number <N>] [--rebase]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Create and inspect task-owned git worktrees."""

import argparse
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-json-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection

_json = tusk_loader.load("tusk-json-lib")
dumps = _json.dumps


def _list_workspaces(conn: sqlite3.Connection) -> list[dict]:
    return _list_workspaces_with_live_state(conn, {})


def _is_stale_workspace(row: dict) -> bool:
    return not row["exists_on_disk"] and row["live_workspace_path"] is None


def _resolve_task_id(raw: str) -> int:
    value = raw.strip()
    if value.upper().startswith("TASK-"):
        value = value[5:]
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid task ID: {raw}") from exc


def _run_git(repo_root: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _detect_default_branch(repo_root: str) -> str:
    set_head = _run_git(repo_root, ["remote", "set-head", "origin", "--auto"])
    if set_head.returncode == 0:
        origin_head = _run_git(repo_root, ["symbolic-ref", "refs/remotes/origin/HEAD"])
        if origin_head.returncode == 0 and origin_head.stdout.strip():
            return origin_head.stdout.strip().replace("refs/remotes/origin/", "")

    for candidate in ("main", "master"):
        exists = _run_git(repo_root, ["show-ref", "--verify", f"refs/heads/{candidate}"])
        if exists.returncode == 0:
            return candidate

    current = _run_git(repo_root, ["branch", "--show-current"])
    if current.returncode == 0 and current.stdout.strip():
        return current.stdout.strip()
    return "main"


def _branch_exists(repo_root: str, branch: str) -> bool:
    result = _run_git(repo_root, ["show-ref", "--verify", f"refs/heads/{branch}"])
    return result.returncode == 0


def _origin_remote_exists(repo_root: str) -> bool:
    result = _run_git(repo_root, ["remote", "get-url", "origin"])
    return result.returncode == 0


def _remote_branch_exists(repo_root: str, branch: str) -> bool:
    result = _run_git(
        repo_root,
        ["show-ref", "--verify", f"refs/remotes/origin/{branch}"],
    )
    return result.returncode == 0


def _resolve_worktree_base(repo_root: str) -> tuple[bool, str, str]:
    default_branch = _detect_default_branch(repo_root)
    if not _origin_remote_exists(repo_root):
        return True, default_branch, ""

    fetch = _run_git(repo_root, ["fetch", "origin"])
    if fetch.returncode != 0:
        return (
            False,
            "",
            "could not refresh origin before creating task workspace:\n"
            f"{fetch.stderr.strip()}",
        )

    default_branch = _detect_default_branch(repo_root)
    if _remote_branch_exists(repo_root, default_branch):
        return True, f"origin/{default_branch}", ""
    return True, default_branch, ""


def _create_worktree(
    repo_root: str,
    worktree_path: str,
    branch: str,
    base_branch: str,
) -> tuple[bool, str]:
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    result = _run_git(
        repo_root,
        ["worktree", "add", "-b", branch, worktree_path, base_branch],
    )
    return result.returncode == 0, result.stderr.strip()


def _attach_worktree(
    repo_root: str,
    worktree_path: str,
    branch: str,
) -> tuple[bool, str]:
    """Re-attach a worktree at ``worktree_path`` checked out on existing ``branch``.

    Mirrors ``_create_worktree`` but omits ``-b`` so an existing branch is
    reused rather than recreated (issue #803). Used when a ``task_workspaces``
    row exists, its branch still resolves in git, but ``workspace_path`` was
    deleted from disk — the canonical recovery path that avoids forcing the
    caller to prune-and-retry.
    """
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    result = _run_git(
        repo_root,
        ["worktree", "add", worktree_path, branch],
    )
    return result.returncode == 0, result.stderr.strip()


def _parse_git_worktrees(repo_root: str) -> dict[str, str]:
    result = _run_git(repo_root, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return {}

    by_branch: dict[str, str] = {}
    current_path = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
        elif line.startswith("branch refs/heads/") and current_path:
            branch = line[len("branch refs/heads/"):].strip()
            by_branch[branch] = current_path
    return by_branch


def _list_workspaces_with_live_state(
    conn: sqlite3.Connection, live_by_branch: dict[str, str]
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, task_id, branch, workspace_path, created_at, updated_at
        FROM task_workspaces
        ORDER BY id
        """
    ).fetchall()
    return [
        {
            "workspace_id": row["id"],
            "task_id": row["task_id"],
            "branch": row["branch"],
            "workspace_path": row["workspace_path"],
            "exists_on_disk": os.path.isdir(row["workspace_path"]),
            "live_workspace_path": live_by_branch.get(row["branch"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _fetch_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, bakeoff_shadow FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _workspace_payload(row: sqlite3.Row, *, created: bool) -> dict:
    return {
        "workspace_id": row["id"],
        "task_id": row["task_id"],
        "branch": row["branch"],
        "workspace_path": row["workspace_path"],
        "created": created,
    }


def cmd_create(db_path: str, repo_root: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk task-worktree create",
        description="Create or reuse a task-owned git worktree.",
    )
    parser.add_argument("task_id", help="Task ID as an integer or TASK-NNN.")
    parser.add_argument("slug", help="Branch slug for feature/TASK-<id>-<slug>.")
    parser.add_argument(
        "--workspace-root",
        default=None,
        help=(
            "Parent directory for task worktrees. Default: $TUSK_WORKTREE_ROOT "
            "or $HOME/.tusk/worktrees."
        ),
    )
    args = parser.parse_args(argv)

    try:
        task_id = _resolve_task_id(args.task_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    slug = args.slug.strip().strip("/")
    if not slug:
        print("Error: Slug must not be empty", file=sys.stderr)
        return 1

    branch = f"feature/TASK-{task_id}-{slug}"
    workspace_root = (
        args.workspace_root
        or os.environ.get("TUSK_WORKTREE_ROOT")
        or os.path.join(os.path.expanduser("~"), ".tusk", "worktrees")
    )
    workspace_path = os.path.join(workspace_root, f"TASK-{task_id}-{slug}")

    conn = get_connection(db_path)
    try:
        task = _fetch_task(conn, task_id)
        if task is None:
            print(f"Error: task {task_id} not found", file=sys.stderr)
            return 1
        if task["bakeoff_shadow"]:
            print(
                f"Error: TASK-{task_id} is a bakeoff shadow; task worktrees "
                "must target normal tasks.",
                file=sys.stderr,
            )
            return 1

        existing = conn.execute(
            """
            SELECT id, task_id, branch, workspace_path
            FROM task_workspaces
            WHERE task_id = ? AND branch = ?
            """,
            (task_id, branch),
        ).fetchone()
        if existing:
            # Healthy state: registry row + workspace_path present on disk.
            if os.path.isdir(existing["workspace_path"]):
                print(dumps(_workspace_payload(existing, created=False)))
                return 0
            # Stale state (issue #803): registry row exists but workspace_path
            # is gone from disk. The caller would otherwise `cd` into a
            # dangling path. Recover when the branch still resolves in git;
            # refuse loudly when it does not.
            if _branch_exists(repo_root, existing["branch"]):
                ok, err = _attach_worktree(
                    repo_root,
                    existing["workspace_path"],
                    existing["branch"],
                )
                if not ok:
                    print(
                        "Error: recorded workspace path is missing on disk and "
                        f"`git worktree add` could not re-attach it:\n"
                        f"  Workspace path: {existing['workspace_path']}\n"
                        f"  Branch:         {existing['branch']}\n"
                        f"  git stderr:     {err}\n"
                        f"  Hint: run `tusk task-worktree prune` to drop the stale row, "
                        f"then re-run `tusk task-worktree create {task_id} {slug}` "
                        f"to materialize a fresh workspace.",
                        file=sys.stderr,
                    )
                    return 2
                print(dumps(_workspace_payload(existing, created=True)))
                return 0
            # Both row and disk and branch are gone — registry is fully stale.
            print(
                "Error: recorded workspace is unusable — both the workspace "
                "path and the branch are missing:\n"
                f"  Workspace path: {existing['workspace_path']}\n"
                f"  Branch:         {existing['branch']}\n"
                f"  Hint: run `tusk task-worktree prune` to drop the stale row, "
                f"then re-run `tusk task-worktree create {task_id} {slug}` "
                f"to materialize a fresh workspace.",
                file=sys.stderr,
            )
            return 2

        if _branch_exists(repo_root, branch):
            print(
                f"Error: branch '{branch}' already exists but is not recorded "
                "as a task workspace.",
                file=sys.stderr,
            )
            return 2

        base_ok, base_branch, base_err = _resolve_worktree_base(repo_root)
        if not base_ok:
            print(f"Error: {base_err}", file=sys.stderr)
            return 2

        ok, err = _create_worktree(
            repo_root,
            workspace_path,
            branch,
            base_branch,
        )
        if not ok:
            print(f"Error: git worktree add failed:\n{err}", file=sys.stderr)
            return 2

        cur = conn.execute(
            """
            INSERT INTO task_workspaces (task_id, branch, workspace_path)
            VALUES (?, ?, ?)
            """,
            (task_id, branch, workspace_path),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, task_id, branch, workspace_path
            FROM task_workspaces
            WHERE id = ?
            """,
            (cur.lastrowid,),
        ).fetchone()
        print(dumps(_workspace_payload(row, created=True)))
        return 0
    except sqlite3.IntegrityError as exc:
        print(f"Error: could not record task workspace: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def cmd_list(db_path: str, repo_root: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk task-worktree list",
        description="List recorded task-owned git worktrees.",
    )
    parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="Output format (default: json).",
    )
    parser.parse_args(argv)

    conn = get_connection(db_path)
    try:
        print(dumps(_list_workspaces_with_live_state(conn, _parse_git_worktrees(repo_root))))
    finally:
        conn.close()
    return 0


def cmd_prune(db_path: str, repo_root: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk task-worktree prune",
        description="Remove stale task-owned worktree registry rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview stale rows without deleting them.",
    )
    parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="Output format (default: json).",
    )
    args = parser.parse_args(argv)

    conn = get_connection(db_path)
    try:
        stale_rows = [
            row
            for row in _list_workspaces_with_live_state(
                conn, _parse_git_worktrees(repo_root)
            )
            if _is_stale_workspace(row)
        ]
        if stale_rows and not args.dry_run:
            conn.executemany(
                "DELETE FROM task_workspaces WHERE id = ?",
                [(row["workspace_id"],) for row in stale_rows],
            )
            conn.commit()
        print(
            dumps(
                {
                    "dry_run": args.dry_run,
                    "removed_count": len(stale_rows),
                    "removed": stale_rows,
                }
            )
        )
    finally:
        conn.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print("Usage: tusk task-worktree list", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path, accepted for dispatcher consistency.
    # argv[2] is repo_root, used by create/status commands.
    command = argv[3] if len(argv) > 3 else ""
    rest = argv[4:]

    repo_root = argv[2]

    if command == "create":
        return cmd_create(db_path, repo_root, rest)
    if command in {"list", "status"}:
        return cmd_list(db_path, repo_root, rest)
    if command == "prune":
        return cmd_prune(db_path, repo_root, rest)

    print(
        "Usage: tusk task-worktree create <task_id> <slug> [--workspace-root <path>]\n"
        "       tusk task-worktree list [--format json]\n"
        "       tusk task-worktree prune [--dry-run] [--format json]",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

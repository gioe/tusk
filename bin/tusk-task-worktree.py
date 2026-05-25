#!/usr/bin/env python3
"""Create and inspect task-owned git worktrees."""

import argparse
import json
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

# Canonical runtime artifacts auto-linked when `worktree.symlink_files` is
# empty (issue #854). install.sh-only installs never run the project_type
# auto-seed in `init-write-config`, leaving the list empty even for projects
# that obviously need these files. The fallback links them anyway and prints
# a stderr advisory pointing at /tusk-update so the implicit list can be made
# explicit; `TUSK_NO_AUTO_SYMLINK=1` disables it.
CANONICAL_RUNTIME_FILES = ["node_modules", ".venv", ".env", ".env.local"]


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


def _primary_repo_root(repo_root: str) -> str:
    """Resolve the primary checkout's root from a possibly-worktree ``repo_root``.

    ``repo_root`` is whatever the dispatcher passed in (cwd-resolved). In a
    linked worktree, ``git --git-common-dir`` points at the primary's ``.git``;
    the parent of that is the primary checkout. In the primary itself, the
    common-dir is the same as the git-dir and the parent IS the primary root.
    Falls back to ``repo_root`` on any git error so symlink seeding is best-
    effort and never breaks worktree creation.
    """
    result = _run_git(
        repo_root,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )
    if result.returncode != 0:
        return repo_root
    common_dir = result.stdout.strip()
    if not common_dir:
        return repo_root
    primary = os.path.dirname(common_dir)
    return primary if os.path.isdir(primary) else repo_root


def _load_symlink_files(config_path: str) -> list[str]:
    """Load ``worktree.symlink_files`` from the project config, returning [] on any error."""
    if not config_path or not os.path.exists(config_path):
        return []
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    worktree_cfg = cfg.get("worktree")
    if not isinstance(worktree_cfg, dict):
        return []
    names = worktree_cfg.get("symlink_files")
    if not isinstance(names, list):
        return []
    # Filter to non-empty strings; ignore None / empty / non-string entries.
    return [str(n) for n in names if isinstance(n, str) and n]


def _link_gitignored_files(
    primary_root: str,
    worktree_path: str,
    names: list[str],
) -> list[dict]:
    """Symlink configured entries from ``primary_root`` into ``worktree_path``.

    Entries are partitioned by shape:

    - **Bare basenames** (no ``/``) — walk ``primary_root`` for files/dirs
      whose basename appears in the configured set. Every match is symlinked
      at the corresponding relative path under ``worktree_path``. Skips
      ``.git``; never follows symlinks during the walk. This is the original
      behavior (issue #752).
    - **Path-style entries** (contain ``/``) — treated as project-relative
      paths. Exactly one symlink is created at ``worktree_path/<entry>``
      pointing back to ``primary_root/<entry>`` iff the primary target exists.
      No walking, no over-matching nested copies — gives monorepo users a way
      to scope (e.g. ``apps/web/node_modules``) without linking every nested
      ``node_modules`` (issue #867).

    Path-style entries are validated: a leading ``/``, an empty segment (``//``
    or trailing ``/``), or any ``.`` / ``..`` segment is rejected silently —
    these could escape the primary checkout or yield ambiguous targets.

    Skips entries whose worktree destination already exists.

    Returns ``[{"src": <primary_path>, "dst": <worktree_path>}, ...]`` for
    each symlink that was actually created.
    """
    if not names:
        return []

    basenames: list[str] = []
    path_entries: list[str] = []
    for name in names:
        if "/" not in name:
            basenames.append(name)
            continue
        if name.startswith("/"):
            continue
        parts = name.split("/")
        if any(p in ("", ".", "..") for p in parts):
            continue
        path_entries.append(name)

    created: list[dict] = []

    def _try_link(src: str, dst: str) -> None:
        # `lexists` catches files, dirs, and symlinks (including broken).
        if os.path.lexists(dst):
            return
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(src, dst)
        except OSError:
            # Best-effort: do not abort worktree creation on a single
            # failed symlink (permission errors, race conditions, etc.).
            return
        created.append({"src": src, "dst": dst})

    # Path-style entries first so a later bare-basename walk that would match
    # the same leaf (e.g. ".venv" basename + "apps/scraper/.venv" path-style)
    # sees the destination already present and skips it.
    for rel in path_entries:
        src = os.path.join(primary_root, rel)
        if not os.path.lexists(src):
            continue
        dst = os.path.join(worktree_path, rel)
        _try_link(src, dst)

    if basenames:
        name_set = set(basenames)
        for root, dirs, files in os.walk(primary_root, followlinks=False):
            if ".git" in dirs:
                dirs.remove(".git")
            # Capture matched dir names BEFORE we mutate `dirs` to control recursion.
            matched_dirs = [d for d in dirs if d in name_set]
            matched_files = [f for f in files if f in name_set]
            for name in matched_dirs + matched_files:
                src = os.path.join(root, name)
                rel = os.path.relpath(src, primary_root)
                dst = os.path.join(worktree_path, rel)
                _try_link(src, dst)
            # Prevent os.walk from descending INTO any directory we just symlinked
            # — the symlink target already contains its full subtree.
            for d in matched_dirs:
                dirs.remove(d)

    return created


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


def _auto_prune_stale_workspaces(
    conn: sqlite3.Connection, repo_root: str, exclude_task_id: int
) -> int:
    """Drop registry rows whose ``workspace_path`` is gone AND not in ``git worktree list``.

    Same staleness predicate as ``tusk task-worktree prune`` (``_is_stale_workspace``),
    scoped to exclude ``exclude_task_id`` so the per-task reconcile logic in
    ``cmd_create`` (re-attach when branch survives, refuse when fully stale) runs
    intact for the current task's own row. Returns the count of rows deleted.
    """
    stale = [
        row
        for row in _list_workspaces_with_live_state(
            conn, _parse_git_worktrees(repo_root)
        )
        if _is_stale_workspace(row) and row["task_id"] != exclude_task_id
    ]
    if stale:
        conn.executemany(
            "DELETE FROM task_workspaces WHERE id = ?",
            [(row["workspace_id"],) for row in stale],
        )
        conn.commit()
    return len(stale)


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


def cmd_create(
    db_path: str, config_path: str, repo_root: str, argv: list[str]
) -> int:
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
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Override path to tusk/config.json for this invocation — use to "
            "verify dispatcher-consumed config changes (e.g. "
            "worktree.symlink_files) from a feature worktree before merging. "
            "Default: primary checkout's tusk/config.json via dispatcher."
        ),
    )
    args = parser.parse_args(argv)

    if args.config is not None:
        if not os.path.isfile(args.config):
            print(
                f"Error: --config path does not exist or is not a regular file: "
                f"{args.config}",
                file=sys.stderr,
            )
            return 1
        config_path = args.config

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

        # Reconcile sibling tasks' stale registry rows before adding a new
        # workspace, so registry accumulation is capped without operator
        # effort (TASK-477). Scoped to ``task_id != exclude_task_id`` so the
        # issue #803 reconcile logic (re-attach when branch survives, refuse
        # when fully stale) for THIS task's own row runs intact.
        if not os.environ.get("TUSK_NO_AUTO_PRUNE"):
            _auto_prune_stale_workspaces(conn, repo_root, task_id)

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
        # Seed gitignored runtime files (e.g. .venv, .env) from the primary
        # repo per worktree.symlink_files config (issue #752), or — when that
        # list is empty and TUSK_NO_AUTO_SYMLINK is unset — fall back to the
        # canonical name set so install.sh-only installs that never ran the
        # init-write-config auto-seed still pick up node_modules / .venv /
        # .env / .env.local (issue #854). Best-effort throughout: individual
        # symlink failures are swallowed inside _link_gitignored_files.
        symlink_names = _load_symlink_files(config_path)
        is_fallback = False
        if not symlink_names and not os.environ.get("TUSK_NO_AUTO_SYMLINK"):
            symlink_names = list(CANONICAL_RUNTIME_FILES)
            is_fallback = True
        if symlink_names:
            primary_root = _primary_repo_root(repo_root)
            created = _link_gitignored_files(
                primary_root, workspace_path, symlink_names
            )
            if is_fallback and created:
                linked_basenames = sorted({os.path.basename(c["dst"]) for c in created})
                print(
                    "Note: auto-linked "
                    + ", ".join(linked_basenames)
                    + " from primary (worktree.symlink_files is empty). "
                    "Run /tusk-update to set the list explicitly, or "
                    "TUSK_NO_AUTO_SYMLINK=1 to disable this fallback.",
                    file=sys.stderr,
                )
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
    config_path = argv[1]
    repo_root = argv[2]
    command = argv[3] if len(argv) > 3 else ""
    rest = argv[4:]

    if command == "create":
        return cmd_create(db_path, config_path, repo_root, rest)
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

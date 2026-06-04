#!/usr/bin/env python3
"""Manage task scope — authoritative declarations supersede the
``task_referenced_paths`` hint cache.

Called by the tusk wrapper:
    tusk scope list <task_id>
    tusk scope add <task_id> <pattern> [--reason TEXT] [--source S]
    tusk scope remove <row_id>
    tusk scope lock <task_id> [--by NAME]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags

Sources (CHECK constraint on ``task_scope.source``):
    auto_derived       — backfilled from task_referenced_paths
    operator_declared  — set via `tusk task-insert --scope <pattern>`
    creates            — set via `tusk task-insert --creates <path>`
    expanded_mid_task  — added by `tusk scope add` (default for this CLI)
    unbounded          — set via `tusk task-insert --unbounded`; signals
                         "no path restriction" to the commit-time scope
                         guard (scope-paths emits no patterns in that case)

Exit codes:
    0 — success (JSON payload on stdout)
    1 — usage error / task not found / DB error
    2 — validation error (bad --source)
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-json-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
get_connection = _db_lib.get_connection
dumps = _json_lib.dumps


VALID_SOURCES_ADD = ("expanded_mid_task", "operator_declared", "creates")
_GLOB_CHARS = frozenset("*?[")


def _parse_task_id(raw: str) -> int:
    s = (raw or "").strip()
    if s.upper().startswith("TASK-"):
        s = s[5:]
    try:
        return int(s)
    except ValueError:
        print(f"Error: invalid task_id: {raw!r}", file=sys.stderr)
        sys.exit(1)


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _ensure_task_exists(conn: sqlite3.Connection, task_id: int) -> None:
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        print(f"Error: task {task_id} not found", file=sys.stderr)
        sys.exit(1)


def _validate_pattern(pattern: str) -> "str | None":
    """Reject patterns the commit-time scope guard could never match.

    The guard does literal repo-root-relative string matching, so absolute
    paths and parent-traversal segments are noise rows that never enforce
    anything. Returning a non-None error string causes cmd_add to exit 2.
    """
    if pattern.startswith("/"):
        return f"Error: pattern must be a repo-root-relative path; got {pattern!r}"
    segments = pattern.split("/")
    if any(seg == ".." for seg in segments):
        return f"Error: pattern must not contain '..' segments; got {pattern!r}"
    return None


def _repo_root(config_path: str) -> str:
    env_root = os.environ.get("TUSK_REPO_ROOT")
    if env_root:
        return os.path.realpath(env_root)

    cfg = os.path.realpath(config_path)
    if os.path.basename(cfg) == "config.default.json":
        return os.path.dirname(cfg)
    if os.path.basename(cfg) == "config.json" and os.path.basename(os.path.dirname(cfg)) == "tusk":
        return os.path.dirname(os.path.dirname(cfg))
    return os.getcwd()


def _is_pattern_like(pattern: str) -> bool:
    return any(ch in pattern for ch in _GLOB_CHARS)


def _normalize_pattern(pattern: str, repo_root: str, source: str) -> tuple[str, "str | None"]:
    """Return a stable repo-root-relative path for plain scope paths.

    ``task_scope`` can also hold pattern-like values, so leave those alone.
    The normal mid-task flow should reference files that already exist; the
    ``creates`` source intentionally names future paths and is exempt.
    """
    if _is_pattern_like(pattern):
        return pattern, None

    normalized = os.path.normpath(pattern)
    if normalized == ".":
        return normalized, "Error: pattern must name a file or directory; got '.'"
    if normalized.startswith("../") or normalized == "..":
        return normalized, f"Error: pattern must not escape the repo root; got {pattern!r}"

    path = Path(repo_root, normalized)
    if source != "creates" and not path.exists():
        return normalized, (
            f"Error: scope path does not exist at repo root: {normalized!r}. "
            "Use --source creates for paths this task will create."
        )

    return normalized, None


def _git(cwd: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _is_sparse_checkout(worktree_root: str) -> bool:
    inside = _git(worktree_root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False
    sparse = _git(worktree_root, ["config", "--get", "core.sparseCheckout"])
    return sparse.returncode == 0 and sparse.stdout.strip() == "true"


def _sparse_cone_entry(pattern: str, repo_root: str) -> "str | None":
    if _is_pattern_like(pattern) or "/" not in pattern:
        return None

    primary_path = Path(repo_root, pattern)
    if primary_path.is_dir():
        entry = pattern
    else:
        entry = os.path.dirname(pattern)

    entry = entry.strip().rstrip("/")
    if not entry or entry == "." or entry.startswith("/"):
        return None
    if any(seg in {"", ".."} for seg in entry.split("/")):
        return None
    return entry


def _materialize_sparse_path(pattern: str, repo_root: str) -> None:
    """Best-effort: keep sparse checkout contents aligned with new scope."""
    worktree_root = os.getcwd()
    if not _is_sparse_checkout(worktree_root):
        return

    target = Path(worktree_root, pattern)
    if target.exists():
        return

    entry = _sparse_cone_entry(pattern, repo_root)
    if entry is None:
        return

    result = _git(worktree_root, ["sparse-checkout", "add", entry])
    if result.returncode == 0:
        print(
            f"Note: sparse-checkout materialized scope path via "
            f"`git sparse-checkout add {entry}`.",
            file=sys.stderr,
        )
        return

    stderr = result.stderr.strip()
    print(
        f"Warning: scope path {pattern!r} may be outside the current "
        f"sparse-checkout cone. Run `git sparse-checkout add {entry}` "
        f"from this worktree to materialize it."
        + (f" git stderr: {stderr}" if stderr else ""),
        file=sys.stderr,
    )


def _has_task_work_evidence(conn: sqlite3.Connection, task_id: int) -> bool:
    """Return True once a task has durable progress or committed criteria."""
    progress = conn.execute(
        "SELECT 1 FROM task_progress WHERE task_id = ? LIMIT 1",
        (task_id,),
    ).fetchone()
    if progress is not None:
        return True

    committed_criterion = conn.execute(
        "SELECT 1 FROM acceptance_criteria "
        "WHERE task_id = ? AND commit_hash IS NOT NULL LIMIT 1",
        (task_id,),
    ).fetchone()
    return committed_criterion is not None


def _resolve_add_source(
    conn: sqlite3.Connection,
    task_id: int,
    requested_source: "str | None",
) -> str:
    if requested_source is not None:
        return requested_source
    if _has_task_work_evidence(conn, task_id):
        return "expanded_mid_task"
    return "operator_declared"


def _task_has_unbounded_scope(conn: sqlite3.Connection, task_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM task_scope "
        "WHERE task_id = ? AND pattern = '**' AND source = 'unbounded' LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def cmd_list(args: argparse.Namespace, db_path: str) -> int:
    task_id = _parse_task_id(args.task_id)
    with get_connection(db_path) as conn:
        _ensure_task_exists(conn, task_id)
        rows = conn.execute(
            "SELECT id, task_id, pattern, source, reason, locked_at, locked_by, created_at "
            "FROM task_scope WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    print(dumps([_row_to_dict(r) for r in rows]))
    return 0


def cmd_add(args: argparse.Namespace, db_path: str, repo_root: str) -> int:
    task_id = _parse_task_id(args.task_id)
    if args.source is not None and args.source not in VALID_SOURCES_ADD:
        joined = ", ".join(VALID_SOURCES_ADD)
        print(
            f"Error: invalid --source {args.source!r}. Valid for `scope add`: {joined}",
            file=sys.stderr,
        )
        return 2
    pattern = (args.pattern or "").strip()
    if not pattern:
        print("Error: <pattern> required", file=sys.stderr)
        return 1
    err = _validate_pattern(pattern)
    if err is not None:
        print(err, file=sys.stderr)
        return 2
    with get_connection(db_path) as conn:
        _ensure_task_exists(conn, task_id)
        if _task_has_unbounded_scope(conn, task_id):
            print(dumps({
                "task_id": task_id,
                "pattern": pattern,
                "source": "unbounded",
                "unbounded": True,
                "note": (
                    "task scope is unbounded; no further authorization needed"
                ),
            }))
            return 0
        source = _resolve_add_source(conn, task_id, args.source)
        pattern, err = _normalize_pattern(pattern, repo_root, source)
        if err is not None:
            print(err, file=sys.stderr)
            return 2
        if source != "creates":
            _materialize_sparse_path(pattern, repo_root)

        existing = conn.execute(
            "SELECT id, task_id, pattern, source, reason, locked_at, locked_by, created_at "
            "FROM task_scope WHERE task_id = ? AND pattern = ? ORDER BY id LIMIT 1",
            (task_id, pattern),
        ).fetchone()
        if existing is not None:
            print(dumps(_row_to_dict(existing)))
            return 0

        conn.execute(
            "INSERT INTO task_scope (task_id, pattern, source, reason) "
            "VALUES (?, ?, ?, ?)",
            (task_id, pattern, source, args.reason),
        )
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        row = conn.execute(
            "SELECT id, task_id, pattern, source, reason, locked_at, locked_by, created_at "
            "FROM task_scope WHERE id = ?",
            (new_id,),
        ).fetchone()
    print(dumps(_row_to_dict(row)))
    return 0


def cmd_lock(args: argparse.Namespace, db_path: str) -> int:
    task_id = _parse_task_id(args.task_id)
    locked_by = args.by or os.environ.get("USER") or "unknown"
    with get_connection(db_path) as conn:
        _ensure_task_exists(conn, task_id)
        # Lock only rows that aren't already locked — re-running is a no-op
        # for previously-locked entries.
        cur = conn.execute(
            "UPDATE task_scope "
            "SET locked_at = datetime('now'), locked_by = ? "
            "WHERE task_id = ? AND locked_at IS NULL",
            (locked_by, task_id),
        )
        rows_locked = cur.rowcount
        conn.commit()
        locked_at_row = conn.execute(
            "SELECT MAX(locked_at) AS locked_at FROM task_scope WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    print(dumps({
        "task_id": task_id,
        "locked_at": locked_at_row["locked_at"],
        "locked_by": locked_by,
        "rows_locked": rows_locked,
    }))
    return 0


def cmd_remove(args: argparse.Namespace, db_path: str) -> int:
    try:
        row_id = int(str(args.row_id).strip())
    except ValueError:
        print(f"Error: invalid row_id: {args.row_id!r}", file=sys.stderr)
        return 1

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, task_id, pattern, source FROM task_scope WHERE id = ?",
            (row_id,),
        ).fetchone()
        if row is None:
            print(f"Error: scope row {row_id} not found", file=sys.stderr)
            return 1

        conn.execute("DELETE FROM task_scope WHERE id = ?", (row_id,))
        conn.commit()

    print(dumps({
        "removed": True,
        "id": row["id"],
        "task_id": row["task_id"],
        "pattern": row["pattern"],
        "source": row["source"],
    }))
    return 0


def main(argv: list) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk-scope.py <db_path> <config_path> <list|add|remove|lock> ...",
            file=sys.stderr,
        )
        return 1

    db_path = argv[1]
    config_path = argv[2]
    repo_root = _repo_root(config_path)

    parser = argparse.ArgumentParser(prog="tusk scope", description="Manage task scope")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List scope entries for a task")
    p_list.add_argument("task_id")

    p_add = sub.add_parser(
        "add",
        help=(
            "Add a scope pattern to a task (default source: operator_declared "
            "before task work, expanded_mid_task afterward)"
        ),
    )
    p_add.add_argument("task_id")
    p_add.add_argument("pattern")
    p_add.add_argument("--reason", default=None)
    p_add.add_argument(
        "--source",
        default=None,
        choices=VALID_SOURCES_ADD,
    )

    p_lock = sub.add_parser(
        "lock",
        help="Stamp locked_at on every scope entry for a task",
    )
    p_lock.add_argument("task_id")
    p_lock.add_argument("--by", default=None, help="Lock attribution (defaults to $USER)")

    p_remove = sub.add_parser(
        "remove",
        aliases=["rm"],
        help="Remove one scope entry by row id",
    )
    p_remove.add_argument("row_id")

    args = parser.parse_args(argv[3:])

    # Catch-all so an uncaught exception (e.g. a transient "database is locked"
    # OperationalError under concurrent access, despite the busy_timeout) leaves
    # an actionable stderr message rather than a bare traceback or — worse — a
    # nonzero exit the silent-exit guard at bin/tusk:73-95 masks with its generic
    # "exited N with no diagnostic output" line (issue #946, mirroring the
    # skill-run guard from issue #785). argparse raises SystemExit, which is not
    # an Exception subclass, so usage errors still propagate unchanged.
    try:
        if args.cmd == "list":
            return cmd_list(args, db_path)
        if args.cmd == "add":
            return cmd_add(args, db_path, repo_root)
        if args.cmd in ("remove", "rm"):
            return cmd_remove(args, db_path)
        if args.cmd == "lock":
            return cmd_lock(args, db_path)

        parser.print_help(sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Error: scope {args.cmd} crashed with "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

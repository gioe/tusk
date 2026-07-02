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

``scope list`` is normally a read-only view of persisted ``task_scope`` rows.
For enforced tasks with no persisted rows, it may return non-persisted
``auto_derived`` fallback rows with ``id=null`` so retro/reporting code can see
the effective text-derived scope without mutating old tasks.

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
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, and tusk-task-update.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_task_update = tusk_loader.load("tusk-task-update")
get_connection = _db_lib.get_connection
dumps = _json_lib.dumps
# Reuse the same auto_derived rebuild path task-update runs on a summary or
# description edit, so `scope rederive` and an inline edit stay consistent.
rederive_auto_scope = _task_update._rederive_auto_scope


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


def _is_pattern_like(pattern: str) -> bool:
    return any(ch in pattern for ch in _GLOB_CHARS)


def _normalize_pattern(pattern: str, worktree_root: str, source: str) -> tuple[str, "str | None"]:
    """Return a stable repo-root-relative path for plain scope paths.

    ``task_scope`` can also hold pattern-like values, so leave those alone.
    The normal mid-task flow should reference files that already exist; the
    ``creates`` source intentionally names future paths and is exempt. The
    existence check resolves against the worktree the command runs in, not the
    primary checkout (issue #1099) — see ``_path_exists_for_scope``.
    """
    if _is_pattern_like(pattern):
        return pattern, None

    normalized = os.path.normpath(pattern)
    if normalized == ".":
        return normalized, "Error: pattern must name a file or directory; got '.'"
    if normalized.startswith("../") or normalized == "..":
        return normalized, f"Error: pattern must not escape the repo root; got {pattern!r}"

    if source != "creates" and not _path_exists_for_scope(normalized, worktree_root):
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


def _worktree_root() -> str:
    """Resolve the working tree the scope command was invoked from.

    Existence checks for plain scope paths must validate against the worktree
    the task actually operates in — NOT the primary checkout. ``bin/tusk``
    exports ``TUSK_REPO_ROOT`` (and passes the config path) as the *primary*
    checkout even when invoked from a linked worktree (the shared-config
    invariant), so the old config-derived root rejected paths that exist on
    ``origin/<default>`` and in the worktree but not yet in a lagging primary
    checkout (issue #1099). The git toplevel of CWD is the ground truth for
    what the task operates on; fall back to CWD when git can't resolve it.
    """
    result = _git(os.getcwd(), ["rev-parse", "--show-toplevel"])
    if result.returncode == 0:
        top = result.stdout.strip()
        if top:
            return os.path.realpath(top)
    return os.path.realpath(os.getcwd())


def _path_exists_for_scope(normalized: str, worktree_root: str) -> bool:
    """Does this repo-root-relative path exist for the task to operate on?

    A path counts as present when it is either materialized on disk in the
    worktree OR tracked in the worktree's ``HEAD`` tree. The HEAD check keeps
    sparse-checkout scope additions working: ``tusk task-worktree create``
    leaves out-of-cone paths unmaterialized on disk, but they are still
    tracked, and ``_materialize_sparse_path`` pulls them into the cone right
    after this check passes. Before issue #1099 the sparse path relied on the
    existence check hitting the full primary checkout; resolving against the
    worktree means that fallback now comes from HEAD instead.
    """
    if Path(worktree_root, normalized).exists():
        return True
    tracked = _git(worktree_root, ["cat-file", "-e", f"HEAD:{normalized}"])
    return tracked.returncode == 0


def _is_sparse_checkout(worktree_root: str) -> bool:
    inside = _git(worktree_root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False
    sparse = _git(worktree_root, ["config", "--get", "core.sparseCheckout"])
    return sparse.returncode == 0 and sparse.stdout.strip() == "true"


def _sparse_cone_entry(pattern: str, root: str) -> "str | None":
    if _is_pattern_like(pattern) or "/" not in pattern:
        return None

    root_path = Path(root, pattern)
    if root_path.is_dir():
        entry = pattern
    else:
        entry = os.path.dirname(pattern)

    entry = entry.strip().rstrip("/")
    if not entry or entry == "." or entry.startswith("/"):
        return None
    if any(seg in {"", ".."} for seg in entry.split("/")):
        return None
    return entry


def _materialize_sparse_path(pattern: str, worktree_root: "str | None" = None) -> None:
    """Best-effort: keep sparse checkout contents aligned with new scope."""
    worktree_root = worktree_root or _worktree_root()
    if not _is_sparse_checkout(worktree_root):
        return

    target = Path(worktree_root, pattern)
    if target.exists():
        return

    entry = _sparse_cone_entry(pattern, worktree_root)
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


def _recorded_task_worktree_root(conn: sqlite3.Connection, task_id: int) -> "str | None":
    row = conn.execute(
        "SELECT workspace_path FROM task_workspaces "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    workspace_path = row["workspace_path"]
    if not workspace_path or not os.path.isdir(workspace_path):
        return None
    return os.path.realpath(workspace_path)


def _scope_validation_root(conn: sqlite3.Connection, task_id: int) -> str:
    return _recorded_task_worktree_root(conn, task_id) or _worktree_root()


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


def _task_scope_enforced(task: sqlite3.Row) -> bool:
    return "scope_enforced" in task.keys() and bool(task["scope_enforced"])


def _effective_auto_derived_rows(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    config_path: str,
) -> list[dict]:
    """Return derived scope rows for old enforced tasks without mutating DB."""
    task_id = task["id"]
    explicit_rows = conn.execute(
        "SELECT pattern FROM task_scope WHERE task_id = ? AND source <> 'auto_derived'",
        (task_id,),
    ).fetchall()
    explicit_patterns = {row["pattern"] for row in explicit_rows}

    criteria = conn.execute(
        "SELECT criterion, verification_spec FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    text_blocks = [task["summary"] or "", task["description"] or ""]
    for criterion in criteria:
        text_blocks.append(criterion["criterion"] or "")
        text_blocks.append(criterion["verification_spec"] or "")

    ti = _task_update._task_insert
    gh = _task_update._git_helpers
    repo_root = ti._repo_root(config_path)
    task_type = task["task_type"] if "task_type" in task.keys() else None
    requires_unit_tests = any(
        ti._UNIT_TEST_REQUIREMENT_RE.search(block or "")
        for block in text_blocks
    )
    seen: set[str] = set()
    rows = []
    for text in text_blocks:
        for path in ti._auto_scope_candidates(
            text,
            repo_root=repo_root,
            task_type=task_type,
            requires_unit_tests=requires_unit_tests,
        ):
            if ti.is_prose_identifier_path(path, repo_root):
                continue
            resolved = ti._resolve_auto_derived_scope_pattern(repo_root, path)
            if not gh.is_trackable_scope_pattern(repo_root, resolved):
                continue
            if resolved in explicit_patterns or resolved in seen:
                continue
            seen.add(resolved)
            rows.append({
                "id": None,
                "task_id": task_id,
                "pattern": resolved,
                "source": "auto_derived",
                "reason": "effective fallback from task text; not persisted",
                "locked_at": None,
                "locked_by": None,
                "created_at": None,
            })
    return rows


def cmd_list(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    task_id = _parse_task_id(args.task_id)
    with get_connection(db_path) as conn:
        _ensure_task_exists(conn, task_id)
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        rows = conn.execute(
            "SELECT id, task_id, pattern, source, reason, locked_at, locked_by, created_at "
            "FROM task_scope WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        payload = [_row_to_dict(r) for r in rows]
        if not payload and task is not None and _task_scope_enforced(task):
            payload = _effective_auto_derived_rows(conn, task, config_path)
    print(dumps(payload))
    return 0


def cmd_add(args: argparse.Namespace, db_path: str) -> int:
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
        worktree_root = _scope_validation_root(conn, task_id)
        pattern, err = _normalize_pattern(pattern, worktree_root, source)
        if err is not None:
            print(err, file=sys.stderr)
            return 2
        if source != "creates":
            _materialize_sparse_path(pattern, worktree_root)

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


def _auto_derived_patterns(conn: sqlite3.Connection, task_id: int) -> set:
    rows = conn.execute(
        "SELECT pattern FROM task_scope "
        "WHERE task_id = ? AND source = 'auto_derived'",
        (task_id,),
    ).fetchall()
    return {r["pattern"] for r in rows}


def _rederive_one(conn: sqlite3.Connection, task_id: int, config_path: str) -> dict:
    """Rebuild one task's ``auto_derived`` rows and return its removed/added diff.

    The single shared per-task path for both ``scope rederive <task_id>`` and
    ``scope rederive --all``: it deletes and rebuilds the task's
    ``auto_derived`` rows via ``_rederive_auto_scope`` while leaving
    ``operator_declared`` / ``creates`` / ``unbounded`` rows untouched, and
    returns the JSON-serializable per-task summary. The caller owns the
    transaction (so the bulk path can commit per task)."""
    before = _auto_derived_patterns(conn, task_id)
    preserved = conn.execute(
        "SELECT id, pattern, source FROM task_scope "
        "WHERE task_id = ? AND source <> 'auto_derived' ORDER BY id",
        (task_id,),
    ).fetchall()
    rederive_auto_scope(conn, task_id, config_path)
    after = _auto_derived_patterns(conn, task_id)
    return {
        "task_id": task_id,
        "removed": sorted(before - after),
        "added": sorted(after - before),
        "auto_derived": sorted(after),
        "preserved": [_row_to_dict(r) for r in preserved],
    }


def _cmd_rederive_all(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Rebuild ``auto_derived`` scope rows across many tasks in one call.

    The bulk variant of ``cmd_rederive``: iterates every open task (or every
    task with ``--include-closed``) and runs the same ``_rederive_one`` path the
    single-task command uses, committing per task so a failure partway through
    keeps prior progress. Emits a per-task removed/added/preserved summary plus a
    processed/changed rollup.
    """
    with get_connection(db_path) as conn:
        if args.include_closed:
            rows = conn.execute("SELECT id FROM tasks ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status <> 'Done' ORDER BY id"
            ).fetchall()
        results = []
        for row in rows:
            results.append(_rederive_one(conn, row["id"], config_path))
            conn.commit()

    changed = [r for r in results if r["removed"] or r["added"]]
    print(dumps({
        "all": True,
        "include_closed": bool(args.include_closed),
        "tasks_processed": len(results),
        "tasks_changed": len(changed),
        "results": results,
    }))
    return 0


def cmd_rederive(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Recompute a task's ``auto_derived`` scope rows from its current text.

    Re-runs the same ``_rederive_auto_scope`` path ``tusk task-update`` runs on
    a summary/description edit, but on demand — so operators can clean up stale
    auto_derived rows (and the spurious ``missing_scope_path`` warnings they
    produce) after the derivation logic changes, without editing the
    description (which the shell-metacharacter guard blocks for issue-sourced
    text). ``operator_declared``, ``creates``, and ``unbounded`` rows are left
    untouched — only ``auto_derived`` rows are deleted and rebuilt.

    Pass ``--all`` (mutually exclusive with a positional ``task_id``) to rebuild
    every open task fleet-wide; see ``_cmd_rederive_all``.
    """
    if args.all and args.task_id is not None:
        print(
            "Error: pass either a task_id or --all, not both",
            file=sys.stderr,
        )
        return 2
    if not args.all and args.task_id is None:
        print("Error: provide a task_id or --all", file=sys.stderr)
        return 1
    if args.all:
        return _cmd_rederive_all(args, db_path, config_path)

    task_id = _parse_task_id(args.task_id)
    with get_connection(db_path) as conn:
        _ensure_task_exists(conn, task_id)
        result = _rederive_one(conn, task_id, config_path)
        conn.commit()

    print(dumps(result))
    return 0


def main(argv: list) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk-scope.py <db_path> <config_path> <list|add|remove|lock|rederive> ...",
            file=sys.stderr,
        )
        return 1

    db_path = argv[1]
    # argv[2] is the primary checkout's config path (the shared-config
    # invariant). Scope existence checks resolve against the worktree the
    # command runs in via _worktree_root(), so the config path is not consumed
    # for path resolution (issue #1099) — but `rederive` does pass it through to
    # _rederive_auto_scope, which resolves the repo root from it to derive
    # candidate paths.
    config_path = argv[2]

    parser = argparse.ArgumentParser(allow_abbrev=False, prog="tusk scope", description="Manage task scope")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", allow_abbrev=False, help="List scope entries for a task")
    p_list.add_argument("task_id")

    p_add = sub.add_parser(
        "add", allow_abbrev=False,
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
        "lock", allow_abbrev=False,
        help="Stamp locked_at on every scope entry for a task",
    )
    p_lock.add_argument("task_id")
    p_lock.add_argument("--by", default=None, help="Lock attribution (defaults to $USER)")

    p_remove = sub.add_parser(
        "remove", allow_abbrev=False,
        aliases=["rm"],
        help="Remove one scope entry by row id",
    )
    p_remove.add_argument("row_id")

    p_rederive = sub.add_parser(
        "rederive", allow_abbrev=False,
        help=(
            "Recompute auto_derived scope rows from the task's current "
            "summary/description/criteria (preserves operator_declared/"
            "creates/unbounded rows). Pass --all to rebuild every open task "
            "fleet-wide instead of a single task_id."
        ),
    )
    p_rederive.add_argument("task_id", nargs="?", default=None)
    p_rederive.add_argument(
        "--all",
        action="store_true",
        help="Rebuild auto_derived rows for every open task (mutually exclusive with task_id)",
    )
    p_rederive.add_argument(
        "--include-closed",
        action="store_true",
        help="With --all, also process Done tasks (default: open tasks only)",
    )

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
            return cmd_list(args, db_path, config_path)
        if args.cmd == "add":
            return cmd_add(args, db_path)
        if args.cmd in ("remove", "rm"):
            return cmd_remove(args, db_path)
        if args.cmd == "lock":
            return cmd_lock(args, db_path)
        if args.cmd == "rederive":
            return cmd_rederive(args, db_path, config_path)

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

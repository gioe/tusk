"""Shared database and config utilities for tusk scripts.

Provides get_connection(), load_config(), and validate_enum() so every
tusk-*.py script can import them from one place instead of duplicating
the logic.

Imported via tusk_loader (hyphenated filename requires it):

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader

    _db_lib = tusk_loader.load("tusk-db-lib")
    get_connection = _db_lib.get_connection
    load_config = _db_lib.load_config      # optional — only scripts that need it
    validate_enum = _db_lib.validate_enum  # optional — validates a value against config list
"""

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time


# Default time (ms) a connection waits on a locked DB before raising
# "database is locked". Without it, a second concurrent writer fails
# *immediately* — the "retryable operation masked by the silent-exit guard"
# failure mode from issue #946, where parallel worktrees / a retro firing
# many tusk calls collide on the shared tasks.db and the operation only
# succeeds when rerun. A short wait lets the in-flight writer's transaction
# commit so the retry happens inside SQLite instead of bubbling up as a
# spurious nonzero exit. Override with TUSK_BUSY_TIMEOUT_MS.
DEFAULT_BUSY_TIMEOUT_MS = 5000

# busy_timeout (above) covers most contention but NOT the lock-upgrade case:
# when a connection already holds a SHARED read lock (e.g. an open SELECT
# cursor) and tries to promote to a writer while another connection holds the
# RESERVED lock, SQLite returns SQLITE_BUSY *immediately* without invoking the
# busy handler, because waiting could deadlock. Under parallel worktree
# sessions sharing one tasks.db that surfaces as a hard "database is locked"
# crash mid-command even with busy_timeout set (issue #1143). The only robust
# remedy is to retry the whole unit of work — dropping the SHARED lock between
# attempts — with bounded exponential backoff. These knobs mirror
# TUSK_BUSY_TIMEOUT_MS: env override → sane default.
DEFAULT_WRITE_RETRIES = 6           # attempts AFTER the first try (override: TUSK_WRITE_RETRIES)
DEFAULT_WRITE_RETRY_BASE_MS = 25    # first backoff; doubles each attempt (override: TUSK_WRITE_RETRY_BASE_MS)
WRITE_RETRY_MAX_DELAY_S = 2.0       # cap on any single backoff sleep


def _busy_timeout_ms() -> int:
    raw = os.environ.get("TUSK_BUSY_TIMEOUT_MS")
    if raw is not None:
        try:
            val = int(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    return DEFAULT_BUSY_TIMEOUT_MS


def _write_retries() -> int:
    raw = os.environ.get("TUSK_WRITE_RETRIES")
    if raw is not None:
        try:
            val = int(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    return DEFAULT_WRITE_RETRIES


def _write_retry_base_ms() -> int:
    raw = os.environ.get("TUSK_WRITE_RETRY_BASE_MS")
    if raw is not None:
        try:
            val = int(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    return DEFAULT_WRITE_RETRY_BASE_MS


def _is_locked_error(exc: BaseException) -> bool:
    """True iff ``exc`` is a transient SQLite lock/busy error worth retrying.

    Matches "database is locked" (SQLITE_BUSY) and "database is busy"; both are
    transient contention, not corruption or schema errors.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def retry_on_locked(operation, *, retries=None, base_ms=None, label=None):
    """Run ``operation`` (a zero-arg callable), retrying on transient
    "database is locked" / "database is busy" ``OperationalError`` with bounded
    exponential backoff.

    This exists because ``PRAGMA busy_timeout`` does NOT cover the lock-upgrade
    case (issue #1143): a connection holding a SHARED read lock that tries to
    promote to a writer while another connection holds RESERVED gets SQLITE_BUSY
    *immediately*, with no busy-handler wait. The only robust remedy is to retry
    the whole unit of work so the SHARED lock is released between attempts —
    hence ``operation`` should open/commit/close its own connection (or be wrapped
    by ``run_write``) rather than reuse a poisoned one.

    On any non-lock error, or once the retry budget is exhausted, the original
    exception is re-raised unchanged so existing per-command catch-alls keep
    classifying it. When the budget is exhausted on a genuine lock, a one-line
    actionable diagnostic is emitted to stderr first so the failure is not a bare
    traceback.
    """
    if retries is None:
        retries = _write_retries()
    if base_ms is None:
        base_ms = _write_retry_base_ms()
    attempt = 0
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc):
                raise
            if attempt >= retries:
                print(
                    f"tusk: database stayed locked after {retries + 1} attempts "
                    f"({label or 'write'}) — another tusk process is holding the "
                    "write lock. Retry in a moment, or raise TUSK_WRITE_RETRIES / "
                    "TUSK_BUSY_TIMEOUT_MS under heavy parallel load.",
                    file=sys.stderr,
                )
                raise
            delay = min((base_ms * (2 ** attempt)) / 1000.0, WRITE_RETRY_MAX_DELAY_S)
            time.sleep(delay)
            attempt += 1


def run_write(db_path: str, fn, *, label=None, retries=None, base_ms=None):
    """Run ``fn(conn)`` against a fresh ``get_connection(db_path)``, retrying the
    whole unit of work on transient lock contention (see ``retry_on_locked``).

    A new connection is opened for every attempt and always closed in a finally,
    so a failed attempt's uncommitted transaction is rolled back and its SHARED
    lock dropped before the next attempt — the property that makes the
    lock-upgrade SQLITE_BUSY (issue #1143) recoverable. ``fn`` must be safe to
    re-run from scratch: on a locked failure nothing was committed, so callers
    that print their success payload only after ``conn.commit()`` are naturally
    idempotent. Returns whatever ``fn`` returns.
    """
    def _attempt():
        conn = get_connection(db_path)
        try:
            return fn(conn)
        finally:
            conn.close()

    return retry_on_locked(_attempt, retries=retries, base_ms=base_ms, label=label)


def open_sqlite(db_path: str, **connect_kwargs) -> sqlite3.Connection:
    """Open a raw sqlite3 connection, emitting an actionable diagnostic instead
    of a raw OperationalError traceback when ``db_path``'s parent directory does
    not exist (issue #1126, generalized to all raw callers by issue #1131).

    "unable to open database file" surfaces when the DB path is unreachable. The
    only way sqlite cannot open/create the file is when its parent directory does
    not exist — sqlite creates the file itself when the directory is present. A
    missing parent dir therefore means we are not inside an initialized tusk
    project (e.g. tusk run from a stray dir, the wrong directory, or a fresh
    checkout before ``tusk init``), so print a one-line diagnostic and
    ``SystemExit(2)``. Any other open failure (a real corruption/permission
    error against an existing dir) is re-raised unchanged so it is never
    silently swallowed.

    ``**connect_kwargs`` are forwarded verbatim to ``sqlite3.connect`` — callers
    that pass ``timeout=2.0`` for fast-fail-on-lock behavior (tusk commit /
    merge / test-precheck) keep those semantics. This is deliberately distinct
    from the ``busy_timeout`` PRAGMA ``get_connection`` applies; raw callers that
    want the bare connection use this helper, while ``get_connection`` layers the
    row_factory / foreign_keys / busy_timeout setup on top.
    """
    try:
        return sqlite3.connect(db_path, **connect_kwargs)
    except sqlite3.OperationalError:
        parent = os.path.dirname(db_path) or "."
        if not os.path.isdir(parent):
            cwd = os.getcwd()
            print(
                f"tusk: could not locate a tusk database (expected at {db_path}).\n"
                f"  {cwd} is not inside an initialized tusk project.\n"
                "  Run from inside a tusk repo, set TUSK_PROJECT=<path> or "
                "TUSK_DB=<path>, or run 'tusk init' to create one.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        raise


def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a SQLite connection with row_factory, foreign keys, and a
    busy_timeout enabled.

    The busy_timeout (issue #946) makes concurrent writers wait for a held
    lock to clear instead of failing instantly with "database is locked".
    Opens through ``open_sqlite`` so the missing-parent diagnostic (issue #1126)
    is applied uniformly with the other raw callers.

    ``isolation_level = "IMMEDIATE"`` (issue #1143) makes Python's implicit
    transactions emit ``BEGIN IMMEDIATE`` before the first write, so the
    RESERVED write lock is acquired up front — where ``busy_timeout`` IS
    honored — instead of as a SHARED→RESERVED promotion later, which SQLite
    refuses to wait on. This eliminates the common lock-upgrade SQLITE_BUSY for
    write paths whose first statement is the write. Paths that interleave an
    open read cursor with a write still need the whole-operation retry layer
    (``run_write`` / ``retry_on_locked``). Explicit ``conn.execute("BEGIN
    IMMEDIATE")`` callers (e.g. tusk-bakeoff.py) are unaffected: Python
    recognizes a literal ``BEGIN`` and does not auto-open a competing
    transaction.
    """
    conn = open_sqlite(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_busy_timeout_ms()}")
    conn.isolation_level = "IMMEDIATE"
    return conn


def resolve_task_workspace(db_path: str, task_id: int) -> str:
    """Return the on-disk workspace_path for ``task_id``.

    Raises ``SystemExit(1)`` with a clear stderr message when the task has no
    ``task_workspaces`` row, or when the row's ``workspace_path`` no longer
    exists on disk. Used by ``tusk version-bump --task-id`` and
    ``tusk changelog-add --task-id`` to route writes to the worktree's
    checkout from any CWD (issue #903).
    """
    try:
        conn = get_connection(db_path)
    except sqlite3.Error as exc:
        print(f"Error: cannot open tusk DB at {db_path}: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        row = conn.execute(
            "SELECT workspace_path FROM task_workspaces WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (int(task_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        print(
            f"Error: --task-id {task_id} has no recorded task workspace. "
            f"Create one with 'tusk task-worktree create {task_id} <slug>' first, "
            "or omit --task-id to bump VERSION in the current checkout.",
            file=sys.stderr,
        )
        sys.exit(1)
    workspace_path = row["workspace_path"]
    if not os.path.isdir(workspace_path):
        print(
            f"Error: --task-id {task_id} workspace {workspace_path!r} no longer exists on disk. "
            "Run 'tusk task-worktree prune' to drop the stale row, then recreate the workspace.",
            file=sys.stderr,
        )
        sys.exit(1)
    return workspace_path


def _resolve_default_branch(repo_root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return "main"
    if result.returncode == 0:
        name = result.stdout.strip().rsplit("/", 1)[-1]
        if name:
            return name
    return "main"


def _current_branch(repo_root: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_recorded_worktree(db_path: str, repo_root: str) -> bool:
    try:
        target = os.path.realpath(repo_root)
    except OSError:
        return False
    try:
        conn = get_connection(db_path)
    except sqlite3.Error:
        return False
    try:
        rows = conn.execute("SELECT workspace_path FROM task_workspaces").fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    for row in rows:
        candidate = row["workspace_path"]
        if not candidate:
            continue
        try:
            if os.path.realpath(candidate) == target:
                return True
        except OSError:
            continue
    return False


def _active_worktree_tasks(db_path: str) -> list[dict]:
    try:
        conn = get_connection(db_path)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT t.id AS task_id, t.summary, tw.workspace_path "
            "FROM task_workspaces tw "
            "JOIN tasks t ON t.id = tw.task_id "
            "WHERE t.status = 'In Progress' "
            "ORDER BY tw.created_at DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for row in rows:
        path = row["workspace_path"]
        if path and os.path.isdir(path):
            out.append(
                {
                    "task_id": row["task_id"],
                    "summary": row["summary"],
                    "workspace_path": path,
                }
            )
    return out


def maybe_advise_primary_no_task_id(db_path: str, repo_root: str, *, command: str) -> None:
    """Emit a stderr hint when ``command`` was invoked from the primary checkout
    on the default branch with no ``--task-id`` while an active task worktree
    exists (issue #923).

    All three conditions must hold:
      a) ``repo_root`` is NOT one of the recorded ``task_workspaces`` rows
         (i.e. CWD walked up to the primary, not a worktree).
      b) HEAD points to the repo's default branch.
      c) At least one task_workspaces row exists whose owning task is
         In Progress AND whose workspace_path exists on disk.

    The advisory is informational — the caller proceeds with whatever
    target it would otherwise write. Silenced by ``TUSK_QUIET=1``. The
    fix is for an autonomous-agent foot-gun (Claude Code resets CWD
    between Bash calls), so the TTY gate other tusk advisories use is
    deliberately omitted here — the audience is agents, not humans.
    """
    if os.environ.get("TUSK_QUIET") == "1":
        return
    if _is_recorded_worktree(db_path, repo_root):
        return
    branch = _current_branch(repo_root)
    if branch is None:
        return
    if branch != _resolve_default_branch(repo_root):
        return
    candidates = _active_worktree_tasks(db_path)
    if not candidates:
        return
    ids = ", ".join(f"TASK-{c['task_id']}" for c in candidates)
    print(
        f"tusk: hint — invoked from primary on default branch; bumping primary "
        f"target via {command}. Active task worktree(s): {ids}. To target one "
        f"of those workspaces instead, re-run with --task-id <N>.",
        file=sys.stderr,
    )


def load_config(config_path: str) -> dict:
    """Load and return the tusk config JSON."""
    with open(config_path) as f:
        return json.load(f)


def validate_enum(value, valid_values: list, field_name: str) -> str | None:
    """Validate a value against a config list. Returns error message or None."""
    if not valid_values:
        return None  # empty list = no validation
    if value not in valid_values:
        joined = ", ".join(valid_values)
        return f"Invalid {field_name} '{value}'. Valid: {joined}"
    return None


def checkpoint_wal(db_path: str, max_retries: int = 3) -> None:
    """Checkpoint and truncate the WAL, retrying if busy readers block it.

    Uses TRUNCATE mode (vs FULL) so the WAL file is zeroed out on success,
    preventing stale WAL data from being rolled back during branch switches
    or file-move sequences. Silently skips if the DB file does not exist.
    """
    if not os.path.exists(db_path):
        return
    print("Checkpointing WAL...", file=sys.stderr)
    last_row = None
    for attempt in range(max_retries):
        try:
            conn = get_connection(db_path)
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"Warning: WAL checkpoint failed: {e} — continuing.", file=sys.stderr)
            return
        last_row = row
        if row is None or (row[0] == 0 and row[1] == row[2]):
            return  # all pages flushed and WAL truncated
        if attempt < max_retries - 1:
            time.sleep(0.2)
    print(
        f"Warning: WAL checkpoint partially blocked after {max_retries} attempts "
        f"(busy={last_row[0]}, log={last_row[1]}, checkpointed={last_row[2]}) — "
        "pages may still be at risk.",
        file=sys.stderr,
    )


@contextlib.contextmanager
def status_transition_trigger_bypassed(conn: sqlite3.Connection):
    """Run a block with the ``validate_status_transition`` trigger dropped.

    Snapshots the trigger DDL, opens a ``BEGIN IMMEDIATE`` transaction, drops
    the trigger, yields to the caller's UPDATEs, then COMMITs (or ROLLBACKs
    on exception). After the transaction finalises, runs ``tusk regen-triggers``
    to reinstall the trigger. If regen-triggers fails (typically when
    ``tusk/config.json`` carries newer keys the installed validator does not
    accept — issues #824 / #831), the snapshot is replayed and a single
    warning is emitted on stderr.

    Both ``bin/tusk-task-unstart.py`` and ``bin/tusk-task-reopen.py`` need
    this choreography. Without the helper the snapshot/restore boilerplate
    has to be re-implemented in every new caller, and instance feedback
    has caught two recurrences already (TASK-414, TASK-426); this helper
    is the third-recurrence fix (issue #844).

    Caller pattern::

        with status_transition_trigger_bypassed(conn):
            conn.execute("UPDATE tasks SET status = 'To Do' WHERE id = ?", (task_id,))
    """
    trigger_row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='trigger' AND name='validate_status_transition'"
    ).fetchone()
    trigger_ddl = trigger_row[0] if trigger_row else None

    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TRIGGER IF EXISTS validate_status_transition")
        try:
            yield
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        regen = subprocess.run(
            ["tusk", "regen-triggers"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if regen.returncode != 0:
            msg = regen.stderr.strip() or regen.stdout.strip() or "(no output)"
            restored = False
            restore_err = None
            if trigger_ddl:
                try:
                    conn.execute(trigger_ddl)
                    restored = True
                except sqlite3.Error as exc:
                    restore_err = str(exc)
            if restored:
                print(
                    f"Warning: tusk regen-triggers failed (exit {regen.returncode}): {msg}\n"
                    "Status-transition guard restored from snapshot; the "
                    "underlying config problem still needs to be fixed "
                    "(run 'tusk regen-triggers' after addressing it).",
                    file=sys.stderr,
                )
            else:
                extra = (
                    f"Snapshot restore also failed: {restore_err}\n"
                    if restore_err
                    else ""
                )
                print(
                    f"Warning: tusk regen-triggers failed (exit {regen.returncode}): {msg}\n"
                    f"{extra}"
                    "Run 'tusk regen-triggers' manually to restore the status-transition guard.",
                    file=sys.stderr,
                )

"""Integration tests for tusk task-unstart.

Covers the happy path (cleanly-orphaned In Progress task -> To Do) plus all
three guard rejection paths: task_progress rows, [TASK-<id>] commits, and an
open task_sessions row. Also exercises the wrong-status and not-found rejection
branches and the without-`--force` confirmation hint, mirroring the coverage
shape used for tusk-task-reopen-style commands.
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_task_unstart = _load("tusk-task-unstart")


def _insert_task(conn: sqlite3.Connection, *, status: str = "In Progress") -> int:
    started_at = "datetime('now')" if status == "In Progress" else "NULL"
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score, started_at)"
        f" VALUES ('test task', ?, 'feature', 'Medium', 'S', 50, {started_at})",
        (status,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_progress(conn: sqlite3.Connection, task_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO task_progress (task_id, commit_hash, commit_message, files_changed, next_steps)"
        " VALUES (?, 'abc1234', 'wip', 'foo.py', 'keep going')",
        (task_id,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_open_session(conn: sqlite3.Connection, task_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO task_sessions (task_id, started_at) VALUES (?, datetime('now'))",
        (task_id,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_closed_session(conn: sqlite3.Connection, task_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO task_sessions (task_id, started_at, ended_at)"
        " VALUES (?, datetime('now', '-1 hour'), datetime('now'))",
        (task_id,),
    )
    conn.commit()
    return cur.lastrowid


def _call(db_path, config_path, *args, no_commits: bool = True, monkeypatch=None):
    """Invoke tusk-task-unstart.main(...) with stdout/stderr captured.

    By default, stub `find_task_commits` to return [] so the git-commit guard
    is inert; tests that need to exercise that guard pass `no_commits=False`
    and pre-stub the function themselves.
    """
    if no_commits and monkeypatch is not None:
        monkeypatch.setattr(tusk_task_unstart, "find_task_commits", lambda *a, **kw: [])
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_unstart.main([str(db_path), str(config_path), *[str(a) for a in args]])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out and out.startswith("{") else None
    return rc, result, err_buf.getvalue()


def test_happy_path_reverts_in_progress_to_todo(db_path, config_path, monkeypatch):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        task_id = _insert_task(conn, status="In Progress")
    finally:
        conn.close()

    rc, result, err = _call(db_path, config_path, task_id, "--force", monkeypatch=monkeypatch)

    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result is not None
    assert result["task"]["status"] == "To Do"
    assert result["task"]["started_at"] is None
    assert result["prior_status"] == "In Progress"

    # Verify the trigger was restored after regen-triggers.
    conn = sqlite3.connect(str(db_path))
    try:
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='validate_status_transition'"
        ).fetchall()
        assert len(triggers) == 1, "validate_status_transition trigger should be regenerated"
    finally:
        conn.close()


def test_without_force_returns_1_with_hint(db_path, config_path, monkeypatch):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
    finally:
        conn.close()

    rc, _, err = _call(db_path, config_path, task_id, monkeypatch=monkeypatch)
    assert rc == 1
    assert "--force" in err


def test_task_not_found_returns_2(db_path, config_path, monkeypatch):
    rc, _, err = _call(db_path, config_path, 99999, "--force", monkeypatch=monkeypatch)
    assert rc == 2
    assert "not found" in err.lower()


def test_task_already_to_do_returns_2(db_path, config_path, monkeypatch):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="To Do")
    finally:
        conn.close()

    rc, _, err = _call(db_path, config_path, task_id, "--force", monkeypatch=monkeypatch)
    assert rc == 2
    assert "task-unstart only reverses" in err.lower() or "in progress" in err.lower()


def test_task_done_returns_2(db_path, config_path, monkeypatch):
    conn = sqlite3.connect(str(db_path))
    try:
        # A Done task still has started_at populated and status terminal.
        cur = conn.execute(
            "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score, started_at, closed_reason, closed_at)"
            " VALUES ('done task', 'Done', 'feature', 'Medium', 'S', 50, datetime('now', '-1 hour'), 'completed', datetime('now'))"
        )
        conn.commit()
        task_id = cur.lastrowid
    finally:
        conn.close()

    rc, _, err = _call(db_path, config_path, task_id, "--force", monkeypatch=monkeypatch)
    assert rc == 2
    assert "task-reopen" in err.lower() or "done" in err.lower()


def test_guard_progress_rows_blocks(db_path, config_path, monkeypatch):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
        _insert_progress(conn, task_id)
    finally:
        conn.close()

    rc, _, err = _call(db_path, config_path, task_id, "--force", monkeypatch=monkeypatch)
    assert rc == 2
    assert "progress checkpoint" in err.lower()

    # Verify status is unchanged.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row[0] == "In Progress"
    finally:
        conn.close()


def test_guard_task_commits_blocks(db_path, config_path, monkeypatch):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
    finally:
        conn.close()

    monkeypatch.setattr(
        tusk_task_unstart,
        "find_task_commits",
        lambda *a, **kw: ["abc1234567890fedcba", "0987654321abcdef000"],
    )

    rc, _, err = _call(db_path, config_path, task_id, "--force", no_commits=False)
    assert rc == 2
    assert "[TASK-" in err
    assert "git commit" in err.lower()


def test_guard_open_session_blocks(db_path, config_path, monkeypatch):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
        _insert_open_session(conn, task_id)
    finally:
        conn.close()

    rc, _, err = _call(db_path, config_path, task_id, "--force", monkeypatch=monkeypatch)
    assert rc == 2
    assert "open session" in err.lower()
    assert "session-close" in err.lower()


def test_closed_session_does_not_block(db_path, config_path, monkeypatch):
    """A previously-closed session should not trigger the open-session guard."""
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
        _insert_closed_session(conn, task_id)
    finally:
        conn.close()

    rc, result, err = _call(db_path, config_path, task_id, "--force", monkeypatch=monkeypatch)
    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result["task"]["status"] == "To Do"
    assert result["task"]["started_at"] is None


# ── prefix-collision file-overlap heuristic (issue #627) ──────────────


def _setup_nested_repo(tmp_path, monkeypatch):
    """Initialize a tusk DB at tmp_path/tusk/tasks.db AND a git repo at tmp_path.

    The script computes ``repo_root = dirname(dirname(db_path))``. Pinning the
    DB at ``tmp_path/tusk/tasks.db`` makes ``repo_root`` resolve to ``tmp_path``,
    so the git history in ``tmp_path`` is what the heuristic walks.

    Mirrors the layout used by tests/unit/test_check_deliverables.py — same
    on-default-branch + feature-branch shape required to exercise the
    file-overlap heuristic via real ``git log`` / ``git show`` calls.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(exist_ok=True)
    db_file = tusk_dir / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"tusk init failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    subprocess.run(
        ["git", "init", "-b", "main", str(tmp_path)],
        check=True, capture_output=True, encoding="utf-8",
    )
    for k, v in (("user.email", "test@example.com"), ("user.name", "Test")):
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", k, v],
            check=True, capture_output=True, encoding="utf-8",
        )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "initial"],
        check=True, capture_output=True, encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "symbolic-ref", "refs/remotes/origin/HEAD",
         "refs/remotes/origin/main"],
        check=True, capture_output=True, encoding="utf-8",
    )
    return db_file


def _git_commit_with_files(repo_root, message, file_specs):
    """Write each (relpath, contents) pair under repo_root, stage, and commit."""
    for relpath, contents in file_specs:
        abs_path = os.path.join(str(repo_root), relpath)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(contents)
        subprocess.run(
            ["git", "-C", str(repo_root), "add", relpath],
            check=True, capture_output=True, encoding="utf-8",
        )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-m", message],
        check=True, capture_output=True, encoding="utf-8",
    )
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout.strip()


def _insert_task_in_progress(db_file, summary, description):
    """Insert an In Progress task with the given summary/description, return id."""
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score, started_at) "
            "VALUES (?, ?, 'In Progress', 'feature', 'Medium', 'S', 50, datetime('now'))",
            (summary, description),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _read_status(db_file, task_id):
    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class TestPrefixCollisionHeuristic:
    """Issue #627: [TASK-N] commits whose diff has no overlap with task scope
    must be ignored as prefix-match false positives, mirroring the
    ``merged_not_closed_low_confidence`` heuristic in tusk-check-deliverables.py."""

    def test_unstart_succeeds_when_historical_commit_unrelated_to_task_paths(
        self, tmp_path, config_path, monkeypatch
    ):
        """Issue #627 reproducer: task description names files A; a [TASK-N]
        commit on the default branch touches unrelated file B → unstart succeeds."""
        db = _setup_nested_repo(tmp_path, monkeypatch)
        task_id = _insert_task_in_progress(
            db,
            summary="Ship ios-libs-contribute skill",
            description="Lives at skills/ios-libs-contribute/SKILL.md and registers in CLAUDE.md.",
        )
        # Historical [TASK-N] commit from a prior task numbering — touches a
        # file the current task knows nothing about.
        _git_commit_with_files(
            tmp_path,
            f"[TASK-{task_id}] Skip branch-naming check gracefully in detached HEAD state",
            [(".claude/hooks/branch-naming.sh", "#!/bin/bash\necho hi\n")],
        )

        rc, result, err = _call(db, config_path, task_id, "--force", no_commits=False)
        assert rc == 0, f"expected 0, got {rc}; stderr={err}"
        assert result is not None
        assert result["task"]["status"] == "To Do"
        assert result["task"]["started_at"] is None

    def test_unstart_refuses_when_commit_diff_overlaps_task_paths(
        self, tmp_path, config_path, monkeypatch
    ):
        """No-regression: a [TASK-N] commit whose diff DOES overlap with a path
        named in the task description still blocks unstart."""
        db = _setup_nested_repo(tmp_path, monkeypatch)
        task_id = _insert_task_in_progress(
            db,
            summary="Patch auth handler",
            description="Fix in apps/api/src/handlers/auth.py to handle expired tokens.",
        )
        _git_commit_with_files(
            tmp_path,
            f"[TASK-{task_id}] real fix",
            [("apps/api/src/handlers/auth.py", "def authenticate(): ...\n")],
        )

        rc, _, err = _call(db, config_path, task_id, "--force", no_commits=False)
        assert rc == 2
        assert "[TASK-" in err
        assert "git commit" in err.lower()
        # Status unchanged.
        assert _read_status(db, task_id) == "In Progress"

    def test_unstart_refuses_when_task_has_no_scope_signal(
        self, tmp_path, config_path, monkeypatch
    ):
        """Conservative default: when the task description and criteria
        reference no paths, the heuristic has no scope signal to compare
        against and the original commit-guard refusal stands. Mirrors
        check-deliverables' empty-scope behavior (issue #606 design note)."""
        db = _setup_nested_repo(tmp_path, monkeypatch)
        task_id = _insert_task_in_progress(
            db,
            summary="Add new feature",
            description="Implement the thing the team agreed on Tuesday.",
        )
        _git_commit_with_files(
            tmp_path,
            f"[TASK-{task_id}] some commit",
            [("notes.txt", "hi\n")],
        )

        rc, _, err = _call(db, config_path, task_id, "--force", no_commits=False)
        assert rc == 2
        assert "git commit" in err.lower()
        assert _read_status(db, task_id) == "In Progress"

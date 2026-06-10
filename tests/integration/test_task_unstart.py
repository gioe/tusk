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
    # issue #1043: the refusal must name the one-command path.
    assert "--close-sessions" in err

    # The flag-less refusal must not have touched the session.
    conn = sqlite3.connect(str(db_path))
    try:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM task_sessions WHERE task_id = ? AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0]
        assert open_count == 1
    finally:
        conn.close()


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


# ── --close-sessions one-command skip path (issue #1043) ──────────────


def _open_session_count(db_path, task_id) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM task_sessions WHERE task_id = ? AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def test_close_sessions_closes_open_session_and_unstarts(db_path, config_path, monkeypatch):
    """--force --close-sessions closes the open session and reverts in one call."""
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
        session_id = _insert_open_session(conn, task_id)
    finally:
        conn.close()

    rc, result, err = _call(
        db_path, config_path, task_id, "--force", "--close-sessions", monkeypatch=monkeypatch
    )
    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result["task"]["status"] == "To Do"
    assert result["task"]["started_at"] is None
    assert result["sessions_closed"] == 1
    assert _open_session_count(db_path, task_id) == 0

    # The closed session row must carry ended_at and a computed duration.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT ended_at, duration_seconds FROM task_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        assert row[0] is not None
        assert row[1] is not None
    finally:
        conn.close()


def test_close_sessions_counts_only_open_sessions(db_path, config_path, monkeypatch):
    """A pre-existing closed session is not re-closed or double-counted.

    The schema's unique index allows at most one OPEN session per task, so
    sessions_closed can only ever be 0 or 1 — this pins that the count
    reflects the open session alone, not historical closed rows.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
        closed_id = _insert_closed_session(conn, task_id)
        prior_ended_at = conn.execute(
            "SELECT ended_at FROM task_sessions WHERE id = ?", (closed_id,)
        ).fetchone()[0]
        _insert_open_session(conn, task_id)
    finally:
        conn.close()

    rc, result, err = _call(
        db_path, config_path, task_id, "--force", "--close-sessions", monkeypatch=monkeypatch
    )
    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result["sessions_closed"] == 1
    assert _open_session_count(db_path, task_id) == 0

    # The historical closed row is untouched.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT ended_at FROM task_sessions WHERE id = ?", (closed_id,)
        ).fetchone()
        assert row[0] == prior_ended_at
    finally:
        conn.close()


def test_close_sessions_noop_when_no_open_sessions(db_path, config_path, monkeypatch):
    """The flag is harmless when there is nothing to close."""
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
        _insert_closed_session(conn, task_id)
    finally:
        conn.close()

    rc, result, err = _call(
        db_path, config_path, task_id, "--force", "--close-sessions", monkeypatch=monkeypatch
    )
    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result["task"]["status"] == "To Do"
    assert result["sessions_closed"] == 0


def test_close_sessions_does_not_bypass_progress_guard(db_path, config_path, monkeypatch):
    """Progress checkpoints still refuse, and the open session is left untouched."""
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
        _insert_progress(conn, task_id)
        _insert_open_session(conn, task_id)
    finally:
        conn.close()

    rc, _, err = _call(
        db_path, config_path, task_id, "--force", "--close-sessions", monkeypatch=monkeypatch
    )
    assert rc == 2
    assert "progress checkpoint" in err.lower()
    assert _open_session_count(db_path, task_id) == 1

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row[0] == "In Progress"
    finally:
        conn.close()


def test_close_sessions_does_not_bypass_commit_guard(db_path, config_path, monkeypatch):
    """[TASK-<id>] commits still refuse, and the open session is left untouched."""
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn, status="In Progress")
        _insert_open_session(conn, task_id)
    finally:
        conn.close()

    monkeypatch.setattr(
        tusk_task_unstart,
        "find_task_commits",
        lambda *a, **kw: ["abc1234567890fedcba"],
    )

    rc, _, err = _call(
        db_path, config_path, task_id, "--force", "--close-sessions", no_commits=False
    )
    assert rc == 2
    assert "git commit" in err.lower()
    assert _open_session_count(db_path, task_id) == 1


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


def _insert_task_in_progress(db_file, summary, description, scope_enforced=0):
    """Insert an In Progress task with the given summary/description, return id.

    Defaults to ``scope_enforced=0`` so the prefix-collision heuristic tests
    below (issue #627) exercise the legacy code path — the fresh-DB column
    default is 1 (commit-time guard), under which those tests would refuse
    rather than fall through the heuristic. Tests that exercise the
    TASK-472 scope_enforced=1 bypass pass ``scope_enforced=1`` explicitly
    via ``_set_scope_enforced``.
    """
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score, started_at, scope_enforced) "
            "VALUES (?, ?, 'In Progress', 'feature', 'Medium', 'S', 50, datetime('now'), ?)",
            (summary, description, scope_enforced),
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


def _set_scope_enforced(db_file, task_id, value):
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "UPDATE tasks SET scope_enforced = ? WHERE id = ?",
            (value, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_scope_enforced_skips_overlap(tmp_path, config_path, monkeypatch):
    """TASK-472: with scope_enforced=1, the file-overlap heuristic is skipped
    and every [TASK-N] commit is treated as authoritative — even one whose
    diff doesn't intersect any path the task names. Same fixture shape as
    test_unstart_succeeds_when_historical_commit_unrelated_to_task_paths;
    the only difference is scope_enforced=1, and the expected rc flips to 2.
    """
    db = _setup_nested_repo(tmp_path, monkeypatch)
    task_id = _insert_task_in_progress(
        db,
        summary="Ship ios-libs-contribute skill",
        description="Lives at skills/ios-libs-contribute/SKILL.md and registers in CLAUDE.md.",
    )
    _set_scope_enforced(db, task_id, 1)
    # Same off-scope commit shape as the legacy fixture above: a [TASK-N]
    # commit whose diff doesn't intersect the description's scope. Under
    # scope_enforced=0 the heuristic treats this as a prefix collision and
    # unstart succeeds; under scope_enforced=1 the commit is trusted and
    # unstart refuses with the standard guard message.
    _git_commit_with_files(
        tmp_path,
        f"[TASK-{task_id}] Skip branch-naming check gracefully in detached HEAD state",
        [(".claude/hooks/branch-naming.sh", "#!/bin/bash\necho hi\n")],
    )

    monkeypatch.setenv("TUSK_FORCE_WARN", "1")
    rc, _, err = _call(db, config_path, task_id, "--force", no_commits=False)
    assert rc == 2, f"expected 2 (commit guard refuses), got {rc}; stderr={err}"
    assert "[TASK-" in err
    assert "git commit" in err.lower()
    assert _read_status(db, task_id) == "In Progress"
    # Bypass stderr note fires (force-emitted via TUSK_FORCE_WARN above).
    assert f"task-unstart bypassed prefix-collision check for TASK-{task_id}" in err


# ── regen-triggers failure restore path (issue #824) ──────────────


class TestRegenTriggersFailureRestore:
    """Issue #824: when `tusk regen-triggers` fails in the finally block,
    task-unstart must restore validate_status_transition from the pre-DROP
    snapshot so the DB is never left without the status-transition guard."""

    @staticmethod
    def _fake_regen_failure(*args, **kwargs):
        """Drop-in replacement for subprocess.run that simulates a failing
        regen-triggers without invoking the real binary. The first positional
        arg is the argv list; only the `tusk regen-triggers` call is
        intercepted."""
        argv = args[0] if args else kwargs.get("args", [])
        if isinstance(argv, list) and len(argv) >= 2 and argv[-1] == "regen-triggers":
            return subprocess.CompletedProcess(
                args=argv,
                returncode=1,
                stdout="",
                stderr="Error: config validator rejected newer keys\n",
            )
        return subprocess.run(*args, **kwargs)

    def test_regen_failure_restores_trigger_from_snapshot(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            task_id = _insert_task(conn, status="In Progress")
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_task_unstart.subprocess, "run", self._fake_regen_failure
        )

        rc, result, err = _call(
            db_path, config_path, task_id, "--force", monkeypatch=monkeypatch
        )

        # The unstart itself still succeeds — the regen failure is a warning,
        # not a fatal error.
        assert rc == 0, f"expected 0, got {rc}; stderr={err}"
        assert result is not None
        assert result["task"]["status"] == "To Do"
        assert result["task"]["started_at"] is None

        # The status-transition guard must still be present in sqlite_master
        # even though regen-triggers failed.
        conn = sqlite3.connect(str(db_path))
        try:
            triggers = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name='validate_status_transition'"
            ).fetchall()
            assert len(triggers) == 1, (
                "validate_status_transition should be restored from the "
                "pre-DROP snapshot when regen-triggers fails"
            )
        finally:
            conn.close()

        # The regen-failure warning must still be surfaced (the underlying
        # config problem is real and the user needs to address it).
        assert "regen-triggers failed" in err
        assert "restored from snapshot" in err

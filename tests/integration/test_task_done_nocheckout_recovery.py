"""Regression tests for issue #848: ``tusk task-done`` auto-mark must use the
same three-layer recovery as ``tusk task-summary``.

Before this change, the auto-mark step on ``tusk task-done --reason completed``
called ``find_task_commits`` directly — a bare ``git log --grep`` — and had no
fetch-retry or fsck-unreachable fallback. After ``tusk merge`` used the
no-checkout fast-forward push path (default branch locked in a sibling
worktree), the [TASK-N] commits could end up reachable only via the local
object store, with no remote-tracking ref advanced and the remote URL
unreachable. ``tusk task-done`` then exited 3 ("not yet marked done") even
though the work was committed and pushed.

These tests exercise the post-no-checkout-push state with real git
operations: a primary checkout + a bare remote + a sibling worktree, the
no-checkout push, then explicit cleanup that mirrors ``tusk merge``. Each
scenario configures the remote-tracking ref + remote URL to force a specific
recovery tier (refresh-fetch vs fsck-unreachable) and asserts that
``tusk task-done`` exits 0 with the open criteria auto-marked.
"""

import importlib.util
import io
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_task_done", os.path.join(BIN, "tusk-task-done.py")
)
tusk_task_done = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_task_done)


def _run(cmd, cwd, check=True, env=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _init_repo(repo_root):
    _run(["git", "init", "-q", "-b", "main"], cwd=repo_root)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo_root)
    _run(["git", "config", "user.name", "Test"], cwd=repo_root)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=repo_root)


@pytest.fixture()
def repo_with_pushed_task(tmp_path):
    """Set up a primary checkout + bare remote + sibling worktree that has pushed
    a [TASK-99] commit directly to origin/main (no-checkout fast-forward), then
    cleaned up the local feature branch as ``tusk merge`` would.

    Returns ``(primary_path, task_commit_sha)``. Mirrors the fixture in
    ``test_task_summary_nocheckout_recovery.py`` so the recovery preconditions
    are identical between the two tests.
    """
    bare = tmp_path / "remote.git"
    _run(["git", "init", "-q", "--bare", str(bare)], cwd=str(tmp_path))

    primary = tmp_path / "primary"
    _run(["git", "clone", "-q", str(bare), str(primary)], cwd=str(tmp_path))
    _init_repo(str(primary))
    _run(["git", "commit", "--allow-empty", "-q", "-m", "[INIT] initial"], cwd=str(primary))
    _run(["git", "push", "-q", "origin", "main"], cwd=str(primary))

    sibling = tmp_path / "sibling"
    _run(
        ["git", "worktree", "add", "-q", str(sibling), "-b", "feature/TASK-99-test"],
        cwd=str(primary),
    )
    _init_repo(str(sibling))

    (sibling / "newfile.txt").write_text("changed\n")
    _run(["git", "add", "newfile.txt"], cwd=str(sibling))
    _run(["git", "commit", "-q", "-m", "[TASK-99] add newfile.txt"], cwd=str(sibling))
    task_sha = _run(["git", "rev-parse", "HEAD"], cwd=str(sibling)).stdout.strip()

    _run(["git", "push", "-q", "origin", "feature/TASK-99-test:main"], cwd=str(sibling))

    _run(["git", "worktree", "remove", "-f", str(sibling)], cwd=str(primary), check=False)
    _run(["git", "branch", "-D", "feature/TASK-99-test"], cwd=str(primary), check=False)

    return str(primary), task_sha


def _setup_db_with_open_task(db_path, config_path, task_id=99, status="In Progress"):
    """Insert a single In Progress task with one open criterion via raw SQL.

    The minimal-surface alternative to invoking ``tusk task-insert``: bypass
    validation, just place the rows. The auto-mark path only needs a task row
    + one ``is_completed=0`` acceptance_criteria row to fire.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks (id, summary, description, status, priority, "
            "task_type, complexity, priority_score, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (task_id, "test task", "", status, "Medium", "feature", "S", 50),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, criterion_type) "
            "VALUES (?, ?, ?)",
            (task_id, "do the thing", "manual"),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at) "
            "VALUES (?, datetime('now'))",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _call_task_done(db_path, config_path, task_id, *args):
    """Invoke ``tusk-task-done.main`` with stdout/stderr captured."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_done.main(
            [str(db_path), str(config_path), str(task_id), *[str(a) for a in args]]
        )
    return rc, out_buf.getvalue(), err_buf.getvalue()


class TestTaskDoneRecoveryParity:
    """The three-layer recovery from issue #845 (refresh-fetch + criterion-hash +
    fsck-unreachable) is now mirrored by ``tusk task-done``'s auto-mark step
    (issue #848). task-done doesn't need the criterion-hash tier — its
    auto-mark runs against *open* criteria with no recorded commit_hash — so
    its layered chain is initial scan → refresh-fetch → fsck-unreachable.
    """

    def test_auto_marks_via_fsck_unreachable_when_remote_broken_and_ref_missing(
        self, repo_with_pushed_task, tmp_path, db_path, config_path, monkeypatch
    ):
        """Issue #848 canonical case: the [TASK-99] commit only lives in the
        local object store; the remote-tracking ref is missing; ``origin`` is
        unreachable. fsck-unreachable is the only thing that can save the
        auto-mark step.
        """
        primary, task_sha = repo_with_pushed_task

        # Force every ref-based scan to come up empty AND the fetch retry to
        # fail silently. Drops refs/remotes/origin/main and re-points origin
        # at a path that does not exist.
        _run(
            ["git", "update-ref", "-d", "refs/remotes/origin/main"],
            cwd=primary,
        )
        broken = str(tmp_path / "no-such-remote.git")
        _run(["git", "remote", "set-url", "origin", broken], cwd=primary)

        # Sanity: the initial --all scan must come up empty before the
        # fallback is the only thing that can save us.
        initial = _run(
            [
                "git", "log", "--all",
                "--grep=[TASK-99]",
                "--fixed-strings",
                "--format=%H",
            ],
            cwd=primary,
        ).stdout.strip()
        assert initial == "", (
            "Test precondition failed: expected --all scan to be empty after "
            f"breaking remote and dropping origin/main; got: {initial}"
        )

        _setup_db_with_open_task(db_path, config_path, task_id=99)

        # Pin TUSK_REPO_ROOT so _repo_root_for_git resolves to the primary
        # checkout (the test DB lives outside any git repo by design).
        monkeypatch.setenv("TUSK_REPO_ROOT", primary)

        rc, _stdout, stderr = _call_task_done(
            db_path, config_path, 99, "--reason", "completed"
        )

        assert rc == 0, (
            f"task-done should have auto-marked the open criterion via the "
            f"fsck-unreachable fallback; exited {rc}.\nstderr: {stderr}"
        )

        # The open criterion is now marked done, bound to the fsck-recovered SHA.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT is_completed, commit_hash FROM acceptance_criteria "
                "WHERE task_id = 99"
            ).fetchone()
            status = conn.execute(
                "SELECT status, closed_reason FROM tasks WHERE id = 99"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 1, "criterion should be marked is_completed=1"
        assert row[1] == task_sha, (
            f"expected criterion to be bound to the fsck-recovered SHA "
            f"{task_sha}; got {row[1]}"
        )
        assert status[0] == "Done"
        assert status[1] == "completed"

    def test_auto_marks_via_refresh_fetch_when_remote_tracking_ref_is_stale(
        self, repo_with_pushed_task, tmp_path, db_path, config_path, monkeypatch
    ):
        """The middle tier: the [TASK-99] commit IS on the remote bare repo's
        ``main`` ref, but the local ``refs/remotes/origin/main`` is stale (the
        no-checkout push didn't advance it). A best-effort
        ``git fetch origin main`` refreshes the ref; the retry --all scan
        then finds the commit. fsck never has to fire.
        """
        primary, task_sha = repo_with_pushed_task

        # Force-stale: rewind the local remote-tracking ref to the pre-push tip.
        # The remote (bare repo) still has the [TASK-99] commit reachable via
        # refs/heads/main, so the fetch will recover it.
        pre_push_sha = _run(
            ["git", "rev-list", "--max-parents=0", "refs/remotes/origin/main"],
            cwd=primary,
        ).stdout.strip()
        _run(
            ["git", "update-ref", "refs/remotes/origin/main", pre_push_sha],
            cwd=primary,
        )

        # Sanity: the initial --all scan must come up empty before the
        # fetch retry is the only thing that can save us.
        initial = _run(
            [
                "git", "log", "--all",
                "--grep=[TASK-99]",
                "--fixed-strings",
                "--format=%H",
            ],
            cwd=primary,
        ).stdout.strip()
        assert initial == "", (
            "Test precondition failed: expected no [TASK-99] commit visible "
            f"before fetch retry; got: {initial}"
        )

        _setup_db_with_open_task(db_path, config_path, task_id=99)
        monkeypatch.setenv("TUSK_REPO_ROOT", primary)

        rc, _stdout, stderr = _call_task_done(
            db_path, config_path, 99, "--reason", "completed"
        )

        assert rc == 0, (
            f"task-done should have auto-marked the open criterion via the "
            f"refresh-fetch fallback; exited {rc}.\nstderr: {stderr}"
        )

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT is_completed, commit_hash FROM acceptance_criteria "
                "WHERE task_id = 99"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == 1
        assert row[1] == task_sha

    def test_happy_path_preserved_when_initial_scan_finds_commit(
        self, repo_with_pushed_task, db_path, config_path, monkeypatch
    ):
        """Performance + behavior guard: when ``git log --all --grep`` already
        finds the [TASK-99] commit (the normal case after ``tusk merge`` left
        ``refs/remotes/origin/main`` correctly advanced), the recovery layers
        must NOT fire — both for cost reasons and to demonstrate that the
        recovery routing doesn't change the happy-path SHA selection.
        """
        primary, task_sha = repo_with_pushed_task

        initial = _run(
            [
                "git", "log", "--all",
                "--grep=[TASK-99]",
                "--fixed-strings",
                "--format=%H",
            ],
            cwd=primary,
        ).stdout.strip()
        assert initial == task_sha, (
            "Test precondition failed: expected --all scan to find the "
            f"[TASK-99] commit; got: {initial!r}"
        )

        _setup_db_with_open_task(db_path, config_path, task_id=99)
        monkeypatch.setenv("TUSK_REPO_ROOT", primary)

        # Spy on the recovery layers — neither should be invoked when the
        # initial scan finds the commit.
        gh = tusk_task_done._git_helpers
        calls: list[str] = []
        real_fetch = gh.try_fetch_default_branch
        real_unreachable = gh.find_unreachable_task_commits

        def _spy_fetch(repo_root):
            calls.append(("fetch", repo_root))
            return real_fetch(repo_root)

        def _spy_unreachable(task_id, repo_root):
            calls.append(("fsck", task_id, repo_root))
            return real_unreachable(task_id, repo_root)

        monkeypatch.setattr(gh, "try_fetch_default_branch", _spy_fetch)
        monkeypatch.setattr(gh, "find_unreachable_task_commits", _spy_unreachable)

        rc, _stdout, stderr = _call_task_done(
            db_path, config_path, 99, "--reason", "completed"
        )

        assert rc == 0, f"task-done happy path exited {rc}.\nstderr: {stderr}"
        assert calls == [], (
            "Initial --all scan should have found the commit; recovery "
            f"layers must not be invoked. Got: {calls}"
        )

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT commit_hash FROM acceptance_criteria WHERE task_id = 99"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == task_sha

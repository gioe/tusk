"""Regression tests for tusk merge: feature branch with zero exclusive commits
over the default branch (issue #562).

When a triage-only task closes without any code changes, the feature branch is
identical to the default branch. Previously, this fell into the
``task_on_default`` path and printed:

    Note: TASK-N commit already on main — feature branch is diverged.

Both clauses are wrong: TASK-N had no commit at all (the head was inherited
from default), and the branch wasn't diverged. tusk merge should detect the
zero-new-commits case via ``git rev-list --count <default>..<branch>`` and
print a distinct, accurate closing message.
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

from tests.integration.conftest import _insert_task, _insert_session

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_merge = _load("tusk-merge")


def _make_run(
    branch_name: str,
    default_branch: str = "main",
    task_id: int = 1,
    record_calls: list | None = None,
):
    """Mock run() for the zero-exclusive-commits feature branch case.

    ``git rev-list --count <default>..<branch>`` returns "0" — the branch has no
    exclusive commits. This is the primary signal for the no-new-commits path.
    """
    calls = record_calls if record_calls is not None else []

    def _run(args, check=True):
        calls.append(list(args))

        if args[:2] == ["git", "diff"] and "--name-only" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "stash", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="No local changes to save", stderr="")
        if args[:2] == ["git", "checkout"] and len(args) == 3:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "pull" in args and "origin" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        # The new no-new-commits detector: git rev-list --count <default>..<branch>
        if args[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(args, 0, stdout="0\n", stderr="")
        # Existing log/cherry checks should never run on the no-new-commits path.
        # If they do, returning empty would also resolve to task_on_default=True,
        # but the new message must come from the rev-list short-circuit, not these.
        if args[:2] == ["git", "log"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["git", "cherry"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "merge", "--ff-only"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] in (["git", "branch", "-d"], ["git", "branch", "-D"]):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "session-close" in args:
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
        if "task-done" in args:
            result_json = json.dumps({
                "task": {"id": task_id, "status": "Done", "closed_reason": "completed"},
                "sessions_closed": 0,
                "unblocked_tasks": [],
            })
            return subprocess.CompletedProcess(args, 0, stdout=result_json, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _run, calls


class TestNoNewCommits:
    """tusk merge handles a feature branch with zero exclusive commits over default."""

    def _setup(self, db_path, monkeypatch):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-triage-only"
        record = []
        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(branch, task_id=task_id, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)
        return task_id, session_id, record

    def test_exits_zero(self, db_path, config_path, monkeypatch):
        task_id, session_id, _ = self._setup(db_path, monkeypatch)
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )
        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"

    def test_prints_no_new_commits_message(self, db_path, config_path, monkeypatch):
        """Prints the distinct 'has no new commits' message — not 'diverged'."""
        task_id, session_id, _ = self._setup(db_path, monkeypatch)
        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )
        out = stderr_buf.getvalue()
        assert "has no new commits on the feature branch" in out, (
            f"Expected new no-new-commits message in stderr:\n{out}"
        )
        assert "diverged" not in out, (
            f"Expected NO 'diverged' wording on the no-new-commits path:\n{out}"
        )
        assert "commit already on main" not in out, (
            f"Expected NO 'commit already on main' wording on the no-new-commits path:\n{out}"
        )

    def test_ff_merge_not_called(self, db_path, config_path, monkeypatch):
        task_id, session_id, record = self._setup(db_path, monkeypatch)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )
        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert not ff_calls, f"Expected git merge --ff-only NOT to be called, got: {ff_calls}"

    def test_log_grep_check_skipped(self, db_path, config_path, monkeypatch):
        """The legacy [TASK-N] grep check must not run on the no-new-commits path."""
        task_id, session_id, record = self._setup(db_path, monkeypatch)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )
        grep_calls = [
            c for c in record
            if c[:2] == ["git", "log"] and any("--grep=" in a for a in c)
        ]
        assert not grep_calls, (
            f"Expected legacy [TASK-N] grep check NOT to be called when no commits exist, got: {grep_calls}"
        )

    def test_task_marked_done(self, db_path, config_path, monkeypatch):
        task_id, session_id, record = self._setup(db_path, monkeypatch)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )
        task_done_calls = [c for c in record if "task-done" in c]
        assert task_done_calls, "Expected task-done to be called"
        result = json.loads(stdout_buf.getvalue())
        assert result["task"]["status"] == "Done"

    def test_branch_force_deleted(self, db_path, config_path, monkeypatch):
        """No-new-commits branch is force-deleted with -D (it shares default's tip)."""
        task_id, session_id, record = self._setup(db_path, monkeypatch)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )
        force_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-D"]]
        assert force_delete_calls, "Expected git branch -D to be called for no-new-commits branch"

"""Regression test for issue #921.

The no-checkout fast-forward path runs ``_cleanup_no_checkout_workspace``
after the push and after ``_close_completed_task`` succeeds. If a worktree
applied a schema migration that bumped ``PRAGMA user_version`` past primary's
``SUPPORTED_SCHEMA_MAX``, primary's bin/tusk and .claude/bin/tusk both refuse
every subsequent operator-flow subcommand (skill-run finish, task-summary,
retro, …) with the schema-mismatch preflight. TASK-464 (issue #880) closed
the happy path by auto-invoking ``tusk sync-main`` before cleanup; on success,
primary is updated and the resolver gap never fires. But when sync-main
fails (pre-existing UU on primary, fetch failure, migrate conflict, …) the
old code path still deleted the worktree — leaving the operator with no
schema-compatible binary anywhere on disk.

This test pins the issue #921 behavior: when ``_maybe_advise_stale_deployed_bin``
returns ``"sync_failed"``, ``_complete_no_checkout_fast_forward`` must skip
``_cleanup_no_checkout_workspace`` so the worktree binary remains reachable.
The merge surfaces exit 3 (partial cleanup) so automation can detect the
deferred state without grepping stderr, matching the TASK-504 contract.

The companion happy-path test pins the inverse: on ``"sync_succeeded"`` (or
``"clean"`` when auto-sync is disabled), cleanup still runs as today.
"""

import importlib.util
import io
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.integration.conftest import _insert_session, _insert_task

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_merge():
    spec = importlib.util.spec_from_file_location(
        "tusk_merge_under_test_921",
        os.path.join(REPO_ROOT, "bin", "tusk-merge.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tusk_merge_module():
    return _load_merge()


@pytest.fixture()
def fallback_only_repo(tmp_path, config_path, monkeypatch):
    """Build a tmp repo whose primary checkout has NO installed tusk binary
    in .claude/bin/ — same fixture shape used by the issue #846 ordering test.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(parents=True, exist_ok=True)
    db_file = tusk_dir / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [os.path.join(REPO_ROOT, "bin", "tusk"), "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return {"db_path": db_file, "repo_root": tmp_path}


def _common_run_mock(args, check=True):
    """Shared subprocess.run stub for the no-checkout fast-forward path.

    Returns plausible CompletedProcess shapes for every git call the merge
    flow makes between branch resolution and cleanup. Anything unrecognized
    falls through to a generic exit-0 default so a single missed call shape
    does not silently break a test (the test asserts on the outcome, not on
    individual mock matches).
    """
    if args[:4] == ["git", "worktree", "list", "--porcelain"]:
        return subprocess.CompletedProcess(
            args, 0,
            stdout=(
                "worktree /tmp/repo-main\n"
                "HEAD abc123\n"
                "branch refs/heads/main\n"
            ),
            stderr="",
        )
    if args[:3] == ["git", "remote", "get-url"]:
        return subprocess.CompletedProcess(
            args, 0, stdout="git@example.com:owner/repo.git\n", stderr="",
        )
    if args[:3] == ["git", "rev-parse", "--verify"]:
        return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
    if args[:3] == ["git", "fetch", "origin"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:3] == ["git", "merge-base", "--is-ancestor"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:2] == ["git", "log"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:2] == ["git", "diff"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:2] == ["git", "push"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:3] == ["git", "config", "--get"]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
    if args[:3] == ["git", "branch", "-D"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if len(args) > 0 and "session-close" in args:
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TestCleanupDeferredWhenSyncMainFails:
    """Issue #921: when auto-sync-main fails, the worktree must be preserved
    so the operator can recover via ``<workspace>/bin/tusk``."""

    def test_advisory_returns_sync_failed_on_nonzero_sync_main(
        self, tmp_path, tusk_merge_module, monkeypatch, capsys,
    ):
        """Unit-shaped pin: the helper's return contract is now part of the
        public surface ``_complete_no_checkout_fast_forward`` reads.
        Mirrors the source-repo layout fixture from the advisory test file."""
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "tusk-foo.py").write_text("source\n", encoding="utf-8")
        (tmp_path / ".claude" / "bin").mkdir(parents=True)
        (tmp_path / ".claude" / "bin" / "tusk-foo.py").write_text("source\n", encoding="utf-8")
        (tmp_path / "tusk").mkdir()
        (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path),
            check=True, capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "."], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )

        db_path = str(tmp_path / "tusk" / "tasks.db")
        monkeypatch.setattr(
            tusk_merge_module, "_run_sync_main",
            lambda *a, **kw: subprocess.CompletedProcess(
                args=["tusk", "sync-main"], returncode=1,
                stdout="", stderr="fatal: refusing to merge unrelated histories\n",
            ),
        )
        monkeypatch.setattr(
            tusk_merge_module, "_maybe_refresh_deployed_bin",
            lambda *a, **kw: False,
        )

        outcome = tusk_merge_module._maybe_advise_stale_deployed_bin(
            db_path, tusk_bin="/fake/tusk",
        )

        assert outcome == "sync_failed", (
            f"Expected 'sync_failed' return; got {outcome!r}"
        )
        # Existing advisory wording must still fire so we don't regress
        # issues #877/#908/#915 alongside this new return-value contract.
        err = capsys.readouterr().err
        assert "auto-sync failed (tusk sync-main exit 1)" in err

    def test_advisory_returns_sync_succeeded_on_zero_sync_main(
        self, tmp_path, tusk_merge_module, monkeypatch, capsys,
    ):
        """Pin the happy-path return so the cleanup path stays unchanged."""
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "tusk-foo.py").write_text("source\n", encoding="utf-8")
        (tmp_path / ".claude" / "bin").mkdir(parents=True)
        (tmp_path / ".claude" / "bin" / "tusk-foo.py").write_text("source\n", encoding="utf-8")
        (tmp_path / "tusk").mkdir()
        (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "config", "user.email", "t@t"], cwd=str(tmp_path),
            check=True, capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "."], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )

        db_path = str(tmp_path / "tusk" / "tasks.db")
        monkeypatch.setattr(
            tusk_merge_module, "_run_sync_main",
            lambda *a, **kw: subprocess.CompletedProcess(
                args=["tusk", "sync-main"], returncode=0,
                stdout='{"default_branch": "main", "success": true}', stderr="",
            ),
        )
        monkeypatch.setattr(
            tusk_merge_module, "_maybe_refresh_deployed_bin",
            lambda *a, **kw: False,
        )

        outcome = tusk_merge_module._maybe_advise_stale_deployed_bin(
            db_path, tusk_bin="/fake/tusk",
        )

        assert outcome == "sync_succeeded"
        err = capsys.readouterr().err
        assert "auto-synced primary to origin/main via tusk sync-main" in err

    def test_advisory_returns_clean_when_auto_sync_disabled(
        self, tmp_path, tusk_merge_module, monkeypatch,
    ):
        """``TUSK_NO_AUTO_SYNC_MAIN=1`` keeps the advisory but never invokes
        sync-main — so the caller must NOT defer cleanup; there is no
        in-flight recovery state to preserve."""
        (tmp_path / "bin").mkdir()
        (tmp_path / ".claude" / "bin").mkdir(parents=True)
        (tmp_path / "tusk").mkdir()
        (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "config", "user.email", "t@t"], cwd=str(tmp_path),
            check=True, capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        (tmp_path / "bin" / "tusk-foo.py").write_text("x\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "."], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=str(tmp_path), check=True,
            capture_output=True, encoding="utf-8",
        )

        monkeypatch.setenv("TUSK_NO_AUTO_SYNC_MAIN", "1")
        db_path = str(tmp_path / "tusk" / "tasks.db")
        outcome = tusk_merge_module._maybe_advise_stale_deployed_bin(
            db_path, tusk_bin="/fake/tusk",
        )
        assert outcome == "clean"

    def test_complete_no_checkout_skips_cleanup_when_sync_failed(
        self, fallback_only_repo, config_path, tusk_merge_module, monkeypatch,
    ):
        """End-to-end: sync-main fails → ``_cleanup_no_checkout_workspace``
        is NOT called and ``main()`` returns exit 3 (partial cleanup).
        This is the core issue #921 contract.
        """
        db_path = fallback_only_repo["db_path"]
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-issue-921"
        monkeypatch.setattr(
            tusk_merge_module, "find_task_branch",
            lambda tid: (branch, None, False),
        )
        monkeypatch.setattr(tusk_merge_module, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge_module, "checkpoint_wal", lambda db: None)
        # Force the worktree-fallback layout so tusk_bin resolution mirrors
        # the original incident shape (primary has no installed binary).
        fake_worktree_bin = (
            "/nonexistent/.tusk/worktrees/TASK-X/.claude/bin/tusk-merge.py"
        )
        monkeypatch.setattr(tusk_merge_module, "__file__", fake_worktree_bin)
        monkeypatch.setattr(
            tusk_merge_module, "_close_completed_task",
            lambda *a, **kw: 0,
        )
        # Replace the advisory helper with a stub that returns "sync_failed"
        # directly — the helper has its own dedicated tests; here we just
        # need to drive the caller's branch.
        monkeypatch.setattr(
            tusk_merge_module, "_maybe_advise_stale_deployed_bin",
            lambda *a, **kw: "sync_failed",
        )
        cleanup_invocations: list[tuple] = []

        def _spy_cleanup(db, tid, br):
            cleanup_invocations.append((db, tid, br))
            return True

        monkeypatch.setattr(
            tusk_merge_module, "_cleanup_no_checkout_workspace", _spy_cleanup,
        )
        # Stub the preservation-advisory emitter so we can assert it
        # actually fired in this branch. The helper itself has separate
        # focused tests further down.
        preservation_calls: list[tuple] = []

        def _spy_preservation(db, tid, br):
            preservation_calls.append((db, tid, br))

        monkeypatch.setattr(
            tusk_merge_module, "_emit_worktree_preservation_advisory",
            _spy_preservation,
        )
        monkeypatch.setattr(tusk_merge_module, "run", _common_run_mock)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge_module.main(
                [
                    str(db_path), str(config_path), str(task_id),
                    "--session", str(session_id),
                ]
            )

        assert rc == 3, (
            f"Expected exit 3 (partial cleanup), got {rc}.\n"
            f"stderr: {stderr_buf.getvalue()}"
        )
        assert cleanup_invocations == [], (
            "Issue #921: _cleanup_no_checkout_workspace MUST be skipped "
            f"when sync-main failed; got {cleanup_invocations!r}"
        )
        assert preservation_calls == [(str(db_path), task_id, branch)], (
            "preservation advisory was not invoked with the expected args"
        )

    def test_complete_no_checkout_runs_cleanup_when_sync_succeeded(
        self, fallback_only_repo, config_path, tusk_merge_module, monkeypatch,
    ):
        """Inverse pin: on the happy path the original cleanup still runs
        and main() returns 0 — no regression in the issue #880 success path.
        """
        db_path = fallback_only_repo["db_path"]
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-issue-921-happy"
        monkeypatch.setattr(
            tusk_merge_module, "find_task_branch",
            lambda tid: (branch, None, False),
        )
        monkeypatch.setattr(tusk_merge_module, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge_module, "checkpoint_wal", lambda db: None)
        fake_worktree_bin = (
            "/nonexistent/.tusk/worktrees/TASK-X/.claude/bin/tusk-merge.py"
        )
        monkeypatch.setattr(tusk_merge_module, "__file__", fake_worktree_bin)
        monkeypatch.setattr(
            tusk_merge_module, "_close_completed_task",
            lambda *a, **kw: 0,
        )
        monkeypatch.setattr(
            tusk_merge_module, "_maybe_advise_stale_deployed_bin",
            lambda *a, **kw: "sync_succeeded",
        )
        cleanup_invocations: list[tuple] = []

        def _spy_cleanup(db, tid, br):
            cleanup_invocations.append((db, tid, br))
            return True

        monkeypatch.setattr(
            tusk_merge_module, "_cleanup_no_checkout_workspace", _spy_cleanup,
        )
        preservation_calls: list[tuple] = []
        monkeypatch.setattr(
            tusk_merge_module, "_emit_worktree_preservation_advisory",
            lambda *a, **kw: preservation_calls.append(a),
        )
        monkeypatch.setattr(tusk_merge_module, "run", _common_run_mock)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge_module.main(
                [
                    str(db_path), str(config_path), str(task_id),
                    "--session", str(session_id),
                ]
            )

        assert rc == 0, (
            f"Expected exit 0 (full success), got {rc}.\n"
            f"stderr: {stderr_buf.getvalue()}"
        )
        assert cleanup_invocations == [(str(db_path), task_id, branch)], (
            "cleanup must still run on sync_succeeded; "
            f"got {cleanup_invocations!r}"
        )
        assert preservation_calls == [], (
            "preservation advisory must not fire on the happy path"
        )

    def test_close_failure_overrides_sync_failed_exit_code(
        self, fallback_only_repo, config_path, tusk_merge_module, monkeypatch,
    ):
        """rc-precedence pin: when ``_close_completed_task`` returned 2
        (task-done failed) AND sync-main also failed, the more severe
        signal wins — main() returns 2, not 3. Same precedence rule the
        original cleanup path used (TASK-504)."""
        db_path = fallback_only_repo["db_path"]
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-issue-921-overrides"
        monkeypatch.setattr(
            tusk_merge_module, "find_task_branch",
            lambda tid: (branch, None, False),
        )
        monkeypatch.setattr(tusk_merge_module, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge_module, "checkpoint_wal", lambda db: None)
        fake_worktree_bin = (
            "/nonexistent/.tusk/worktrees/TASK-X/.claude/bin/tusk-merge.py"
        )
        monkeypatch.setattr(tusk_merge_module, "__file__", fake_worktree_bin)
        monkeypatch.setattr(
            tusk_merge_module, "_close_completed_task",
            lambda *a, **kw: 2,
        )
        monkeypatch.setattr(
            tusk_merge_module, "_maybe_advise_stale_deployed_bin",
            lambda *a, **kw: "sync_failed",
        )
        monkeypatch.setattr(
            tusk_merge_module, "_cleanup_no_checkout_workspace",
            lambda *a, **kw: True,
        )
        monkeypatch.setattr(
            tusk_merge_module, "_emit_worktree_preservation_advisory",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(tusk_merge_module, "run", _common_run_mock)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = tusk_merge_module.main(
                [
                    str(db_path), str(config_path), str(task_id),
                    "--session", str(session_id),
                ]
            )

        assert rc == 2, (
            f"close-rc precedence: expected rc=2 (task-done failed), got {rc}."
        )


class TestPreservationAdvisoryNamesRecoveryHandle:
    """``_emit_worktree_preservation_advisory`` must surface a workable
    recovery command — the absolute path to a tusk binary that's known to
    work against the current schema."""

    def _build_workspace_with_bin(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "bin").mkdir(parents=True)
        bin_path = ws / "bin" / "tusk"
        bin_path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        bin_path.chmod(0o755)
        return ws, bin_path

    def test_emits_recovery_handle_when_workspace_has_bin_tusk(
        self, tmp_path, tusk_merge_module, monkeypatch, capsys,
    ):
        ws, bin_path = self._build_workspace_with_bin(tmp_path)
        db_path = str(tmp_path / "tusk" / "tasks.db")
        (tmp_path / "tusk").mkdir()
        (tmp_path / "tusk" / "tasks.db").write_bytes(b"")

        class FakeRow(dict):
            def __getitem__(self, key):
                return super().__getitem__(key)

        monkeypatch.setattr(
            tusk_merge_module, "_recorded_task_workspace",
            lambda db, tid: FakeRow(
                {"id": 1, "branch": "feature/TASK-921", "workspace_path": str(ws)},
            ),
        )

        tusk_merge_module._emit_worktree_preservation_advisory(
            db_path, 921, "feature/TASK-921",
        )

        err = capsys.readouterr().err
        # The preservation note names the worktree path and the issue.
        assert f"leaving worktree {ws}" in err
        assert "Issue #921" in err
        # The recovery handle is the absolute bin path inside the worktree.
        assert str(bin_path) in err
        assert "Recovery handle: invoke operator-flow tusk commands via" in err
        # The follow-up instructions name the manual cleanup commands.
        assert f"git worktree remove --force {ws}" in err
        assert "git branch -D feature/TASK-921" in err
        # And the rerun-the-merge recovery path.
        assert "rerun tusk merge 921" in err

    def test_silent_when_no_recorded_workspace(
        self, tmp_path, tusk_merge_module, monkeypatch, capsys,
    ):
        """A missing registry row means there's nothing to preserve.
        Stay silent rather than emit a misleading recovery pointer."""
        (tmp_path / "tusk").mkdir()
        (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
        db_path = str(tmp_path / "tusk" / "tasks.db")
        monkeypatch.setattr(
            tusk_merge_module, "_recorded_task_workspace",
            lambda db, tid: None,
        )

        tusk_merge_module._emit_worktree_preservation_advisory(
            db_path, 999, "feature/TASK-999",
        )

        assert capsys.readouterr().err == ""

    def test_silent_when_workspace_path_no_longer_exists(
        self, tmp_path, tusk_merge_module, monkeypatch, capsys,
    ):
        """Registry says the workspace exists, but the directory is gone.
        The advisory must not point operators at a phantom path."""
        (tmp_path / "tusk").mkdir()
        (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
        db_path = str(tmp_path / "tusk" / "tasks.db")
        ghost_path = str(tmp_path / "this-path-does-not-exist")

        class FakeRow(dict):
            def __getitem__(self, key):
                return super().__getitem__(key)

        monkeypatch.setattr(
            tusk_merge_module, "_recorded_task_workspace",
            lambda db, tid: FakeRow(
                {"id": 1, "branch": "feature/TASK-999", "workspace_path": ghost_path},
            ),
        )

        tusk_merge_module._emit_worktree_preservation_advisory(
            db_path, 999, "feature/TASK-999",
        )

        assert capsys.readouterr().err == ""

    def test_emits_preservation_note_even_without_bin_handle(
        self, tmp_path, tusk_merge_module, monkeypatch, capsys,
    ):
        """If the worktree exists but none of the canonical bin/ paths
        carry an executable tusk, the preservation note still fires
        (the worktree itself may still be useful for the operator's
        diagnosis) — but the Recovery-handle line is omitted because we
        have nothing concrete to point at."""
        ws = tmp_path / "ws-bare"
        ws.mkdir()
        # No bin/ subdirectory — deliberately empty worktree shell.
        db_path = str(tmp_path / "tusk" / "tasks.db")
        (tmp_path / "tusk").mkdir()
        (tmp_path / "tusk" / "tasks.db").write_bytes(b"")

        class FakeRow(dict):
            def __getitem__(self, key):
                return super().__getitem__(key)

        monkeypatch.setattr(
            tusk_merge_module, "_recorded_task_workspace",
            lambda db, tid: FakeRow(
                {"id": 1, "branch": "feature/TASK-921-bare", "workspace_path": str(ws)},
            ),
        )

        tusk_merge_module._emit_worktree_preservation_advisory(
            db_path, 921, "feature/TASK-921-bare",
        )

        err = capsys.readouterr().err
        assert f"leaving worktree {ws}" in err
        assert "Recovery handle:" not in err, (
            "Recovery-handle line must be omitted when no executable bin/tusk "
            "exists in the worktree"
        )
        # Manual-cleanup instructions still fire because they're correct
        # regardless of whether a bin was found.
        assert f"git worktree remove --force {ws}" in err

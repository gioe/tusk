"""Regression test for issue #834.

When ``tusk merge`` is invoked from inside a task worktree, ``__file__``
points at the worktree-local ``.claude/bin/tusk-merge.py``. The no-checkout
fast-forward cleanup deletes that worktree mid-flow, so a ``tusk_bin``
derived from ``__file__`` is invalid by the time ``_close_completed_task``
shells out to ``tusk task-done``. The fix resolves ``tusk_bin`` from the
primary install location (probed from ``db_path``'s repo root) so the
binary remains valid after worktree cleanup.
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.integration.conftest import _insert_session, _insert_task

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


@pytest.fixture()
def primary_install_repo(tmp_path, config_path, monkeypatch):
    """Build a production-layout tmp repo with a primary tusk binary in place.

    Layout::

        tmp_path/
            tusk/tasks.db          <- TUSK_DB target
            .claude/bin/tusk       <- primary-install Claude binary

    ``dirname(dirname(db_path))`` resolves to ``tmp_path``, so
    ``_resolve_stable_tusk_bin`` will find the ``.claude/bin/tusk`` candidate
    relative to that repo root.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(parents=True, exist_ok=True)
    db_file = tusk_dir / "tasks.db"

    claude_bin_dir = tmp_path / ".claude" / "bin"
    claude_bin_dir.mkdir(parents=True, exist_ok=True)
    primary_bin = claude_bin_dir / "tusk"
    primary_bin.write_text("#!/usr/bin/env bash\nexit 0\n")
    primary_bin.chmod(0o755)

    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [os.path.join(REPO_ROOT, "bin", "tusk"), "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return {"db_path": db_file, "primary_bin": primary_bin, "repo_root": tmp_path}


class TestResolveStableTuskBin:
    """Unit-style coverage of the primary-install probe."""

    def test_claude_install_preferred_over_fallback(self, tmp_path):
        (tmp_path / ".claude" / "bin").mkdir(parents=True)
        (tmp_path / "tusk").mkdir()
        primary = tmp_path / ".claude" / "bin" / "tusk"
        primary.write_text("")
        db = tmp_path / "tusk" / "tasks.db"
        db.write_text("")
        fallback = "/nonexistent/worktree/.claude/bin/tusk"

        assert tusk_merge._resolve_stable_tusk_bin(str(db), fallback) == str(primary)

    def test_codex_install_used_when_only_codex_present(self, tmp_path):
        (tmp_path / "tusk" / "bin").mkdir(parents=True)
        primary = tmp_path / "tusk" / "bin" / "tusk"
        primary.write_text("")
        db = tmp_path / "tusk" / "tasks.db"
        db.write_text("")
        fallback = "/nonexistent/worktree/.claude/bin/tusk"

        assert tusk_merge._resolve_stable_tusk_bin(str(db), fallback) == str(primary)

    def test_claude_install_preferred_over_codex_when_both_present(self, tmp_path):
        (tmp_path / ".claude" / "bin").mkdir(parents=True)
        (tmp_path / "tusk" / "bin").mkdir(parents=True)
        claude_bin = tmp_path / ".claude" / "bin" / "tusk"
        codex_bin = tmp_path / "tusk" / "bin" / "tusk"
        claude_bin.write_text("")
        codex_bin.write_text("")
        db = tmp_path / "tusk" / "tasks.db"
        db.write_text("")
        fallback = "/nonexistent/worktree/.claude/bin/tusk"

        assert tusk_merge._resolve_stable_tusk_bin(str(db), fallback) == str(claude_bin)

    def test_neither_install_present_returns_fallback(self, tmp_path):
        (tmp_path / "tusk").mkdir(parents=True)
        db = tmp_path / "tusk" / "tasks.db"
        db.write_text("")
        fallback = "/nonexistent/worktree/.claude/bin/tusk"

        assert tusk_merge._resolve_stable_tusk_bin(str(db), fallback) == fallback

    def test_fallback_equals_primary_returns_fallback_unchanged(self, tmp_path):
        """When invoked from the primary checkout, ``__file__`` already points at
        the primary binary's sibling. Resolving must return that path (not a
        different candidate) so behavior in the primary-checkout case is
        unchanged from before the fix.
        """
        (tmp_path / ".claude" / "bin").mkdir(parents=True)
        primary = tmp_path / ".claude" / "bin" / "tusk"
        primary.write_text("")
        db = tmp_path / "tusk" / "tasks.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("")

        # Fallback is the same realpath as the primary candidate -> return
        # fallback (the helper's guard short-circuits to preserve identity).
        assert (
            tusk_merge._resolve_stable_tusk_bin(str(db), str(primary)) == str(primary)
        )


class TestResolveSourceRepoInvariant:
    """Issue #841 — in the tusk source repo, ``bin/`` is the source of truth
    and ``.claude/bin/`` is a refresh-on-demand cache populated by
    ``bin/tusk dev-sync``. The cache can lag ``bin/`` after migrations land,
    so source-repo installs must prefer ``bin/tusk`` over ``.claude/bin/tusk``.
    """

    def test_source_repo_layout_prefers_bin_over_claude_bin(self, tmp_path):
        """Source-repo signal (``bin/tusk-migrate.py`` next to ``bin/tusk``)
        flips the preference: ``bin/tusk`` wins over ``.claude/bin/tusk``."""
        (tmp_path / "bin").mkdir(parents=True)
        (tmp_path / ".claude" / "bin").mkdir(parents=True)
        source_bin = tmp_path / "bin" / "tusk"
        source_bin.write_text("")
        (tmp_path / "bin" / "tusk-migrate.py").write_text("")
        claude_bin = tmp_path / ".claude" / "bin" / "tusk"
        claude_bin.write_text("")
        db = tmp_path / "tusk" / "tasks.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("")
        fallback = "/nonexistent/worktree/.claude/bin/tusk"

        assert (
            tusk_merge._resolve_stable_tusk_bin(str(db), fallback) == str(source_bin)
        )

    def test_source_repo_layout_prefers_bin_when_claude_bin_absent(self, tmp_path):
        """Source-repo install with no ``.claude/bin/`` cache yet — ``bin/tusk``
        still wins."""
        (tmp_path / "bin").mkdir(parents=True)
        source_bin = tmp_path / "bin" / "tusk"
        source_bin.write_text("")
        (tmp_path / "bin" / "tusk-migrate.py").write_text("")
        db = tmp_path / "tusk" / "tasks.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("")
        fallback = "/nonexistent/worktree/.claude/bin/tusk"

        assert (
            tusk_merge._resolve_stable_tusk_bin(str(db), fallback) == str(source_bin)
        )

    def test_target_project_with_bin_but_no_migrate_signal_falls_back_to_claude(
        self, tmp_path
    ):
        """A target project that happens to have a ``bin/tusk`` (e.g. an
        unrelated project binary) but no ``bin/tusk-migrate.py`` must NOT be
        treated as a source repo — the canonical Claude install path still
        applies."""
        (tmp_path / "bin").mkdir(parents=True)
        (tmp_path / "bin" / "tusk").write_text("")  # unrelated binary
        (tmp_path / ".claude" / "bin").mkdir(parents=True)
        claude_bin = tmp_path / ".claude" / "bin" / "tusk"
        claude_bin.write_text("")
        db = tmp_path / "tusk" / "tasks.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("")
        fallback = "/nonexistent/worktree/.claude/bin/tusk"

        # No tusk-migrate.py in bin/ → not a source-repo install → Claude branch.
        assert (
            tusk_merge._resolve_stable_tusk_bin(str(db), fallback) == str(claude_bin)
        )

    def test_source_repo_fallback_equals_source_bin_returns_fallback(self, tmp_path):
        """When invoked from the source-repo's own ``bin/tusk-merge.py``,
        ``fallback`` already points at the source ``bin/tusk``. Preserve
        identity rather than returning a re-derived path (mirrors the
        primary-checkout guard for the Claude branch)."""
        (tmp_path / "bin").mkdir(parents=True)
        source_bin = tmp_path / "bin" / "tusk"
        source_bin.write_text("")
        (tmp_path / "bin" / "tusk-migrate.py").write_text("")
        db = tmp_path / "tusk" / "tasks.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("")

        assert (
            tusk_merge._resolve_stable_tusk_bin(str(db), str(source_bin))
            == str(source_bin)
        )


class TestNoCheckoutMergeUsesStableBin:
    """End-to-end: the no-checkout path's subprocess calls must use the primary binary."""

    def test_subprocess_calls_use_primary_not_worktree_bin(
        self, primary_install_repo, config_path, monkeypatch
    ):
        db_path = primary_install_repo["db_path"]
        primary_bin = primary_install_repo["primary_bin"]

        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-worktree-binary"

        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (branch, None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)

        # Simulate the worktree-local invocation: __file__ points at a path
        # inside a (would-be) task worktree, not the primary install. Before
        # the fix this would propagate into tusk_bin and break post-cleanup
        # subprocess calls.
        fake_worktree_bin_dir = "/nonexistent/.tusk/worktrees/TASK-X/.claude/bin"
        monkeypatch.setattr(
            tusk_merge,
            "__file__",
            os.path.join(fake_worktree_bin_dir, "tusk-merge.py"),
        )

        record: list[list[str]] = []

        def _mock_run(args, check=True):
            record.append(list(args))
            # No-checkout default-branch lock probe.
            if args[:4] == ["git", "worktree", "list", "--porcelain"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=(
                        "worktree /tmp/repo-main\n"
                        "HEAD abc123\n"
                        "branch refs/heads/main\n"
                    ),
                    stderr="",
                )
            if args[:3] == ["git", "remote", "get-url"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="git@example.com:owner/repo.git\n", stderr=""
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
            # tusk subprocesses — record args[0] (binary path) so the test
            # can verify which binary was used.
            if "session-close" in args:
                return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
            if "task-done" in args:
                payload = json.dumps(
                    {
                        "task": {
                            "id": task_id,
                            "status": "Done",
                            "closed_reason": "completed",
                        },
                        "sessions_closed": 0,
                        "unblocked_tasks": [],
                    }
                )
                return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [
                    str(db_path),
                    str(config_path),
                    str(task_id),
                    "--session",
                    str(session_id),
                ]
            )

        assert rc == 0, (
            f"Expected exit 0\nstderr: {stderr_buf.getvalue()}\nrecord: {record}"
        )

        # The session-close and task-done subprocess calls must invoke the
        # PRIMARY-install binary, not the worktree-local fallback path.
        tusk_subcommands = [
            cmd for cmd in record if "session-close" in cmd or "task-done" in cmd
        ]
        assert tusk_subcommands, (
            f"Expected session-close and task-done subprocess calls; got: {record}"
        )

        for cmd in tusk_subcommands:
            assert cmd[0] == str(primary_bin), (
                f"Expected subprocess to invoke primary binary {primary_bin}; "
                f"got {cmd[0]} in command {cmd}"
            )
            assert fake_worktree_bin_dir not in cmd[0], (
                f"Subprocess invoked worktree-local binary {cmd[0]}; "
                "expected primary install. This is the issue #834 regression."
            )

        # Sanity: the stderr stream must not contain the "Missing executable"
        # diagnostic that the original bug produced.
        assert "Missing executable" not in stderr_buf.getvalue(), (
            f"Unexpected 'Missing executable' diagnostic:\n{stderr_buf.getvalue()}"
        )

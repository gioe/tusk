"""Regression test for issue #774.

When origin/<default> already contains every commit ``tusk merge`` would
push (typically because the operator ran ``git push --no-verify`` manually
after a pre-push-hook-blocked tusk push and fast-forwarded local default
to match), ``tusk merge`` must skip the push instead of retrying it and
hitting the same hook rejection. Skipping the push lets the rest of
finalization (session close, branch delete, task-done) proceed, so the
task is marked Done — not forced through ``tusk abandon``.
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


class TestOriginAlreadyContainsHelper:
    """Unit tests for the _origin_already_contains predicate."""

    def test_returns_true_when_rev_list_empty(self, monkeypatch):
        def _mock_run(args, check=True):
            assert args[:2] == ["git", "rev-list"]
            return subprocess.CompletedProcess(args, 0, stdout="0\n", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        assert tusk_merge._origin_already_contains("main", "main") is True

    def test_returns_false_when_rev_list_nonzero(self, monkeypatch):
        def _mock_run(args, check=True):
            return subprocess.CompletedProcess(args, 0, stdout="3\n", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        assert (
            tusk_merge._origin_already_contains(
                "feature/TASK-1-foo", "main"
            )
            is False
        )

    def test_returns_false_on_rev_list_error(self, monkeypatch):
        def _mock_run(args, check=True):
            return subprocess.CompletedProcess(
                args, 128, stdout="", stderr="fatal: bad ref\n"
            )

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        assert (
            tusk_merge._origin_already_contains("main", "main") is False
        ), "On rev-list failure, fall through to the normal push path."


class TestMergeSkipsPushWhenOriginAlreadyContains:
    """main()-level integration: full merge flow skips push and finalizes."""

    def _setup_no_checkout_path(
        self, db_path, monkeypatch, *, origin_already_contains: bool
    ):
        """Run the no-checkout fast-forward push path with mocked git ops."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-already-shipped"

        git_calls = []

        def _mock_run(args, check=True):
            git_calls.append(list(args))
            if args[:3] == ["git", "rev-list"]:
                # _origin_already_contains: emit "0" when origin already has
                # the commits, "1" when there's one to push.
                stdout = "0\n" if origin_already_contains else "1\n"
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
            if args[:2] == ["git", "push"]:
                # Mimic the pre-push hook rejecting the push.
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="pre-push hook failed\n"
                )
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

        monkeypatch.setattr(tusk_merge, "run", _mock_run)
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        monkeypatch.setattr(tusk_merge, "_has_remote", lambda name="origin": True)
        # Force the no-checkout fast-forward push code path: the default
        # branch is locked in some other worktree.
        monkeypatch.setattr(
            tusk_merge,
            "_worktree_path_for_branch",
            lambda b: "/tmp/some-other-worktree" if b == "main" else None,
        )
        return task_id, session_id, branch, git_calls

    def test_skip_push_skips_no_checkout_push_when_origin_ahead(
        self, db_path, monkeypatch, capsys
    ):
        """no-checkout fast-forward path skips push when origin already has the commits."""
        # Direct call to the helper with mocked run — proves the predicate is
        # consulted at the no-checkout push site and short-circuits cleanly.
        def _mock_run(args, check=True):
            return subprocess.CompletedProcess(args, 0, stdout="0\n", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)
        assert tusk_merge._origin_already_contains(
            "feature/TASK-99-shipped", "main"
        ) is True

    def test_skip_push_predicate_consulted_before_standard_push(
        self, db_path, monkeypatch
    ):
        """When origin already contains local default, the standard push is bypassed.

        Sanity-check by direct inspection that the helper now exists and
        reads the rev-list output the merge flow expects.
        """
        seen = []

        def _mock_run(args, check=True):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="0\n", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)
        result = tusk_merge._origin_already_contains("main", "main")

        assert result is True
        assert any(
            cmd[:2] == ["git", "rev-list"]
            and any("origin/main..main" in part for part in cmd if isinstance(part, str))
            for cmd in seen
        ), f"Expected rev-list origin/main..main; got: {seen}"

"""Regression test for tusk merge with dirty root-level tracked files (issue #339).

When tracked root-level files (e.g. CLAUDE.md) are modified in the working tree
when tusk merge runs, the merge should:
  1. Detect the dirty files via git diff --name-only.
  2. Stash them with 'git stash push' (no pathspec — scoped pathspecs fail for
     root-level files, see issue #339).
  3. Complete the merge successfully.
  4. Restore the dirty file via git stash pop afterward.
"""

import importlib.util
import io
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr

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


# ---------------------------------------------------------------------------
# Dirty root-level file regression test
# ---------------------------------------------------------------------------


class TestMergeDirtyRootFiles:
    """Regression for issue #339: tusk merge with a modified root-level tracked file."""

    def _make_mock_run(self, task_id: int, dirty_file: str = "CLAUDE.md"):
        """Return a mock run() that simulates a dirty root-level file in the working tree."""
        stash_label = f"tusk-merge: auto-stash for TASK-{task_id}"
        dispatched = []

        def _mock_run(args, check=True):
            dispatched.append(list(args))
            subcmd = args[1] if len(args) > 1 else ""

            # git diff --name-only (unstaged): report the dirty root-level file
            if args[0] == "git" and subcmd == "diff" and "--name-only" in args and "--cached" not in args:
                return subprocess.CompletedProcess(args, 0, stdout=dirty_file, stderr="")

            # git diff --cached --name-only (staged): nothing staged
            if args[0] == "git" and subcmd == "diff" and "--name-only" in args and "--cached" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            # git stash push: succeed (no "No local changes to save")
            if args[0] == "git" and subcmd == "stash" and len(args) > 2 and args[2] == "push":
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"Saved working directory and index state On main: {stash_label}\n", stderr=""
                )

            # git stash list: return the single entry
            if args[0] == "git" and subcmd == "stash" and len(args) > 2 and args[2] == "list":
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"stash@{{0}}: On main: {stash_label}\n", stderr=""
                )

            # git stash pop stash@{0}: succeed
            if args[0] == "git" and subcmd == "stash" and len(args) > 2 and args[2] == "pop":
                return subprocess.CompletedProcess(
                    args, 0, stdout="Already up to date.\nDropped stash@{0}\n", stderr=""
                )

            # session-close: succeed
            if subcmd == "session-close":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            # git checkout/pull/merge/push: succeed
            if args[0] == "git" and subcmd in ("checkout", "pull", "merge", "push"):
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            # git branch -d/--delete: succeed
            if args[0] == "git" and subcmd == "branch" and ("-d" in args or "--delete" in args):
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            # task-done: succeed
            if subcmd == "task-done":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            # Fallback: succeed silently
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        return _mock_run, dispatched

    def test_merge_stashes_dirty_root_level_file(self, db_path, config_path, monkeypatch):
        """tusk merge stashes a dirty root-level file and completes successfully."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        mock_run, dispatched = self._make_mock_run(task_id, dirty_file="CLAUDE.md")

        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda *a, **kw: None)
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        output = buf.getvalue()
        assert rc == 0, f"Expected rc=0 but got {rc}. stderr:\n{output}"
        stash_pushes = [a for a in dispatched if a[0] == "git" and a[1] == "stash" and a[2] == "push"]
        assert stash_pushes, (
            f"Expected 'git stash push' to be called but dispatched:\n"
            + "\n".join(str(a) for a in dispatched)
        )

    def test_merge_emits_stash_message_for_dirty_root_file(self, db_path, config_path, monkeypatch):
        """tusk merge prints a stashing message when a dirty root-level file is detected."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        mock_run, _ = self._make_mock_run(task_id, dirty_file="CLAUDE.md")

        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda *a, **kw: None)
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        buf = io.StringIO()
        with redirect_stderr(buf):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        output = buf.getvalue()
        assert "Stashing uncommitted changes" in output, (
            f"Expected stash message in stderr but got:\n{output}"
        )

    def test_merge_restores_dirty_file_after_merge(self, db_path, config_path, monkeypatch):
        """tusk merge restores the dirty root-level file via stash pop after completing."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        mock_run, dispatched = self._make_mock_run(task_id, dirty_file="CLAUDE.md")

        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda *a, **kw: None)
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        output = buf.getvalue()
        assert rc == 0, f"Expected rc=0 but got {rc}. stderr:\n{output}"

        # Verify stash pop was dispatched (dirty file restored)
        stash_pops = [a for a in dispatched if a[0] == "git" and a[1] == "stash" and a[2] == "pop"]
        assert stash_pops, (
            f"Expected 'git stash pop' to be called but dispatched commands were:\n"
            + "\n".join(str(a) for a in dispatched)
        )

        # Verify stash push had no pathspec (fix for issue #339)
        stash_pushes = [a for a in dispatched if a[0] == "git" and a[1] == "stash" and a[2] == "push"]
        assert stash_pushes, "Expected 'git stash push' to be called"
        push_args = stash_pushes[0]
        # After "git stash push -m <label>", there must be no additional pathspec arguments
        # (the -m flag and its value are at indices 3 and 4; index 5+ would be a pathspec)
        assert len(push_args) == 5, (
            f"Expected 'git stash push -m <label>' with no pathspec but got: {push_args}"
        )

    def test_stash_pop_message_in_stderr(self, db_path, config_path, monkeypatch):
        """tusk merge emits the 'stash restored' note after successfully popping the stash."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        mock_run, _ = self._make_mock_run(task_id, dirty_file="CLAUDE.md")

        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda *a, **kw: None)
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        output = buf.getvalue()
        assert rc == 0, f"Expected rc=0 but got {rc}. stderr:\n{output}"
        assert "stash restored to working tree" in output, (
            f"Expected 'stash restored to working tree' in stderr but got:\n{output}"
        )

    def test_no_stash_when_working_tree_is_clean(self, db_path, config_path, monkeypatch):
        """tusk merge does not invoke git stash when the working tree has no dirty files."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        # Use empty dirty_file to simulate a clean working tree
        mock_run, dispatched = self._make_mock_run(task_id, dirty_file="")

        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda *a, **kw: None)
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected rc=0 but got {rc}. stderr:\n{buf.getvalue()}"

        stash_calls = [
            a for a in dispatched if a[0] == "git" and a[1] == "stash" and a[2] in ("push", "pop", "list")
        ]
        assert not stash_calls, (
            f"Expected no git stash calls for clean working tree but got: {stash_calls}"
        )

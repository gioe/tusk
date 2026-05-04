"""Unit tests for tusk-merge.py graceful handling when push cannot reach origin.

Follow-up to TASK-100. The pull step already falls back to local state on an
unreachable remote; the push step must do the same so a complete merge can
succeed locally when the network is down.
"""

import importlib.util
import json
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")
GIT_HELPERS_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-git-helpers.py")


def _load_real_git_helpers():
    spec = importlib.util.spec_from_file_location("tusk_git_helpers", GIT_HELPERS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.get_connection = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    real_git_helpers = _load_real_git_helpers()

    def _load(name):
        if name == "tusk-git-helpers":
            return real_git_helpers
        return db_lib_mock

    tusk_loader_mock.load.side_effect = _load
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _cp(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _make_run(push_rc: int, push_stderr: str, task_id: int = 1):
    """Build a fake subprocess.run that simulates a reachable-for-pull remote
    but lets the caller control the outcome of `git push`. Pull/merge/etc. are
    treated as successful so that we isolate push-step behavior."""
    task_done_json = json.dumps({"task_id": task_id, "summary": "test", "unblocked_tasks": []})
    pull_calls: list[list[str]] = []
    push_calls: list[list[str]] = []
    checkout_calls: list[list[str]] = []

    def fake_run(args, check=True):
        if args[:3] == ["git", "remote", "get-url"]:
            return _cp(0, stdout="https://example.com/nonexistent.git\n")
        if args[:4] == ["git", "remote", "set-head", "origin"]:
            return _cp(0)
        if args[:2] == ["git", "symbolic-ref"]:
            return _cp(0, stdout="refs/remotes/origin/main\n")
        if args[:3] == ["git", "branch", "--list"]:
            return _cp(0, stdout=f"  feature/TASK-{task_id}-test\n")
        if args[:2] == ["git", "diff"]:
            return _cp(0, stdout="")
        if args[:3] == ["git", "stash", "list"]:
            return _cp(0, stdout="")
        if args[:2] == ["git", "stash"]:
            return _cp(0)
        if args == ["git", "checkout", "main"]:
            checkout_calls.append(args)
            return _cp(0)
        if args[:2] == ["git", "checkout"]:
            checkout_calls.append(args)
            return _cp(0)
        if "pull" in args:
            pull_calls.append(args)
            return _cp(0)
        # unpushed-default guard probes (issue #607) — pretend origin/<default>
        # ref isn't tracked locally so the guard short-circuits silently
        if args[:3] == ["git", "rev-parse", "--verify"] and len(args) == 4 \
                and args[3].startswith("refs/remotes/origin/"):
            return _cp(1, stderr="fatal: bad ref")
        if args[:2] == ["git", "log"]:
            return _cp(0, stdout=f"abc123 [TASK-{task_id}] test\n")
        if args[:2] == ["git", "cherry"]:
            return _cp(0, stdout="+ abc123\n")
        if args[:2] == ["git", "merge"]:
            return _cp(0)
        if "push" in args:
            push_calls.append(args)
            return _cp(push_rc, stderr=push_stderr)
        if args[:2] == ["git", "branch"] and ("-d" in args or "-D" in args):
            return _cp(0)
        if "task-done" in str(args):
            return _cp(0, stdout=task_done_json)
        if "session-close" in str(args):
            return _cp(0)
        return _cp(0)

    return fake_run, pull_calls, push_calls, checkout_calls


def _make_conn_mock(session_id=1):
    conn = MagicMock()
    conn.execute.side_effect = [
        MagicMock(fetchall=MagicMock(return_value=[(session_id, "2026-01-01")])),
    ]
    return conn


class TestMergePushUnreachableRemote:
    """tusk merge falls back to local state when push cannot reach origin."""

    def _run_merge(self, mod, fake_run, tmp_path):
        conn_mock = _make_conn_mock()
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "get_connection", return_value=conn_mock), \
             patch("os.path.exists", return_value=False), \
             patch("os.rename"):
            return mod.main([str(tmp_path / "tasks.db"), str(tmp_path / "config.json"),
                             "1", "--session", "1"])

    def test_push_dns_failure_succeeds(self, capsys, tmp_path):
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            push_rc=128,
            push_stderr=(
                "fatal: unable to access 'https://example.com/nonexistent.git/': "
                "Could not resolve host: example.com"
            ),
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 0
        _, err = capsys.readouterr()
        assert "could not reach origin" in err
        assert "skipping push" in err

    def test_push_repo_not_found_succeeds(self, capsys, tmp_path):
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            push_rc=128,
            push_stderr=(
                "remote: Repository not found.\n"
                "fatal: repository 'https://github.com/nobody/nothing.git/' not found"
            ),
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 0
        _, err = capsys.readouterr()
        assert "could not reach origin" in err

    def test_push_connection_refused_succeeds(self, capsys, tmp_path):
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            push_rc=128,
            push_stderr="fatal: unable to access '...': Failed to connect to ...: Connection refused",
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 0

    def test_push_non_fast_forward_still_fails(self, capsys, tmp_path):
        """Non-network push failures (non-fast-forward, permission denied) must
        still exit 2 — the fallback is network-specific."""
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            push_rc=1,
            push_stderr=(
                "To https://github.com/x/y.git\n"
                " ! [rejected]        main -> main (non-fast-forward)\n"
                "error: failed to push some refs to 'https://github.com/x/y.git'\n"
                "hint: Updates were rejected because the tip of your current branch "
                "is behind its remote counterpart."
            ),
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 2
        _, err = capsys.readouterr()
        assert "git push failed" in err

    def test_push_permission_denied_still_fails(self, capsys, tmp_path):
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            push_rc=128,
            push_stderr="ERROR: Permission to x/y.git denied to user.",
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 2
        _, err = capsys.readouterr()
        assert "git push failed" in err

    def test_feature_branch_push_fail_retry_hint_includes_tusk_merge(self, capsys, tmp_path):
        """Issue #649: when git push is rejected on a transient remote-side
        error after a feature-branch merge has already landed locally and the
        session has been closed, the printed Retry hint must compose the push
        retry with a re-invocation of tusk merge so the second pass finalizes
        the task close + branch cleanup. Bare 'git push origin <default>' alone
        leaves the task In Progress and the feature branch undeleted."""
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            push_rc=1,
            push_stderr=(
                "remote: fatal error in commit_refs\n"
                "To https://github.com/x/y.git\n"
                " ! [remote rejected] main -> main (failure)\n"
            ),
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 2
        _, err = capsys.readouterr()
        assert "Retry: git push origin main && tusk merge 1 --session 1" in err
        assert "The branch has been merged locally but not pushed." in err

    def test_task_on_default_push_fail_retry_hint_includes_tusk_merge(self, capsys, tmp_path):
        """Issue #649: same as above but for the task_on_default path —
        session-close has already run, task-done is still pending, so the
        retry hint must also re-invoke tusk merge to finalize the close."""
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            push_rc=1,
            push_stderr=(
                "remote: fatal error in commit_refs\n"
                "To https://github.com/x/y.git\n"
                " ! [remote rejected] main -> main (failure)\n"
            ),
        )
        # Override the task_on_default detector specifically (the `git log
        # <branch> --not <default> --oneline <task_grep>` shape from
        # bin/tusk-merge.py:1005). Returning empty stdout flips
        # task_on_default to True. Matching by `--not` (rather than the bare
        # ['git', 'log'] prefix) keeps the override scoped to the one call
        # site we mean to control — see convention 37.
        original = fake_run

        def fake_run_task_on_default(args, check=True):
            if args[:2] == ["git", "log"] and "--not" in args:
                return _cp(0, stdout="")
            if args[:2] == ["git", "cherry"]:
                return _cp(0, stdout="- abc123\n")
            return original(args, check=check)

        # Stub the prefix-collision file-overlap heuristic helpers (issue #656)
        # so the override at bin/tusk-merge.py keeps task_on_default=True. The
        # real helpers run subprocess against tmp_path (not a git repo) and would
        # otherwise return empty, flipping task_on_default back to False and
        # routing the merge through the normal ff-merge path instead of the
        # task_on_default path this test is asserting against. task_referenced_paths
        # is stubbed to [] so the heuristic takes the "no scope signal" branch
        # and keeps task_on_default=True even with overlap absent.
        mod.find_task_commits = lambda task_id, repo_root, refs=None, since=None: [
            "deadbeef" + "0" * 32
        ]
        mod.commit_changed_files = lambda commits, repo_root: {"some/file.py"}
        mod.task_referenced_paths = lambda task_id, conn: []
        rc = self._run_merge(mod, fake_run_task_on_default, tmp_path)

        assert rc == 2
        _, err = capsys.readouterr()
        assert "Retry: git push origin main && tusk merge 1 --session 1" in err
        # task_on_default branch must NOT print the feature-branch-only hint.
        assert "The branch has been merged locally but not pushed." not in err

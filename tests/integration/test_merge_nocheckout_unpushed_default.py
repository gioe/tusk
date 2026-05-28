"""Regression tests for issue #949.

When ``task-worktree create`` bases a feature branch on ``origin/<default>``,
any commit that lives only on the LOCAL ``<default>`` ref (e.g. an unpushed
"Upgrade tusk to vN" base commit) is never on the feature branch. The
no-checkout fast-forward path ships ``<branch>:<default>``, so without a guard
those commits are silently stranded on local ``<default>`` while ``tusk merge``
reports success.

The standard checkout path already runs ``_local_default_unpushed_commits`` /
``_confirm_proceed_with_unpushed`` (issue #607). These tests cover the
no-checkout path, which historically skipped that guard:

- abort with exit 2 (no push) when local ``<default>`` has unpushed commits
- the abort names the stranded SHA + subject and a remediation path
- the merge proceeds (and pushes) when local ``<default>`` == ``origin/<default>``
"""

import importlib.util
import io
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout

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


def _complete_kwargs(branch, *, use_rebase=True, session_was_closed=True):
    return dict(
        branch_name=branch,
        default_branch="main",
        task_id=523,
        session_id=900,
        tusk_bin="/usr/bin/true",
        db_path="/tmp/does-not-matter.db",
        session_was_closed=session_was_closed,
        did_stash=False,
        use_rebase=use_rebase,
    )


def _mock_run_factory(*, branch, unpushed_commits, record):
    """Mock ``run`` for the no-checkout fast-forward path.

    unpushed_commits=[]   → local main == origin/main (guard silent)
    unpushed_commits=[..] → local main is ahead of origin/main by those commits
    """

    def _run(args, check=True):
        record.append(list(args))
        if args[:3] == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["git", "checkout"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "rebase", "origin/main"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        # _origin_already_contains: rev-list origin/main..<branch> --count
        if args[:2] == ["git", "rev-list"] and any(
            isinstance(a, str) and a == f"origin/main..{branch}" for a in args
        ):
            return subprocess.CompletedProcess(args, 0, stdout="1\n", stderr="")
        # _local_default_unpushed_commits step 1: rev-parse --verify origin ref
        if (
            args[:3] == ["git", "rev-parse", "--verify"]
            and len(args) == 4
            and args[3] == "refs/remotes/origin/main"
        ):
            return subprocess.CompletedProcess(
                args, 0, stdout="0123456789abcdef\n", stderr=""
            )
        # _local_default_unpushed_commits step 2: log origin/main..main
        if (
            args[:2] == ["git", "log"]
            and "--format=%h %s" in args
            and any(a == "refs/remotes/origin/main..main" for a in args)
        ):
            stdout = "".join(f"{sha} {subject}\n" for sha, subject in unpushed_commits)
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _run


class TestNoCheckoutUnpushedGuard:
    def test_aborts_when_local_default_has_unpushed_commits(self, monkeypatch):
        """The stranded-base-commit scenario from issue #949: abort, do not push."""
        branch = "feature/TASK-523-x"
        unpushed = [("5a3d74c", "Upgrade tusk to v1036")]
        record = []
        monkeypatch.setattr(
            tusk_merge, "run", _mock_run_factory(
                branch=branch, unpushed_commits=unpushed, record=record
            )
        )

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge._complete_no_checkout_fast_forward(**_complete_kwargs(branch))

        assert rc == 2, f"expected abort exit 2, got {rc}\n{stderr_buf.getvalue()}"
        stderr = stderr_buf.getvalue()
        assert "5a3d74c" in stderr
        assert "Upgrade tusk to v1036" in stderr
        assert "stranded" in stderr.lower()
        # Crucially, the destination ref must NOT have been pushed.
        push_calls = [
            c for c in record
            if c[:2] == ["git", "push"]
            and any(isinstance(a, str) and a.endswith(":main") for a in c)
        ]
        assert not push_calls, f"push must not run after the guard aborts: {push_calls}"

    def test_proceeds_when_local_default_clean(self, monkeypatch):
        """When local main == origin/main the guard is silent and the push runs."""
        branch = "feature/TASK-523-x"
        record = []
        monkeypatch.setattr(
            tusk_merge, "run", _mock_run_factory(
                branch=branch, unpushed_commits=[], record=record
            )
        )
        # Bound the happy path: stub out finalization beyond the push.
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        monkeypatch.setattr(
            tusk_merge, "_delete_remote_feature_branch_if_tracking", lambda b: None
        )
        monkeypatch.setattr(tusk_merge, "_warn_branch_auto_stash", lambda tid: None)
        monkeypatch.setattr(tusk_merge, "_resolve_merge_base", lambda *a: "base_sha")
        monkeypatch.setattr(tusk_merge, "_resolve_local_ref_sha", lambda r: "tip_sha")
        monkeypatch.setattr(
            tusk_merge, "_close_completed_task", lambda *a, **k: 0
        )
        monkeypatch.setattr(
            tusk_merge, "_maybe_refresh_deployed_bin", lambda *a, **k: False
        )
        monkeypatch.setattr(
            tusk_merge, "_maybe_advise_stale_deployed_bin", lambda *a, **k: None
        )
        monkeypatch.setattr(
            tusk_merge, "_cleanup_no_checkout_workspace", lambda *a, **k: True
        )

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge._complete_no_checkout_fast_forward(**_complete_kwargs(branch))

        assert rc == 0, f"expected success, got {rc}\n{stderr_buf.getvalue()}"
        push_calls = [
            c for c in record
            if c[:2] == ["git", "push"]
            and any(isinstance(a, str) and a.endswith(":main") for a in c)
        ]
        assert push_calls, "clean state must push the feature branch to main"
        assert "stranded" not in stderr_buf.getvalue().lower()


class TestWarnNoCheckoutUnpushedDefaultMessage:
    def test_message_names_commits_and_remediation(self):
        commits = [
            ("5a3d74c", "Upgrade tusk to v1036"),
            ("deadbee", "[TASK-9999] Unrelated tweak"),
        ]
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            tusk_merge._warn_no_checkout_unpushed_default(commits, "main", 523, 900)
        out = stderr_buf.getvalue()
        # Names each stranded commit
        assert "5a3d74c" in out and "Upgrade tusk to v1036" in out
        assert "deadbee" in out and "[TASK-9999]" in out
        # Loud abort + both safe remediation paths
        assert "Aborting TASK-523" in out
        assert "stranded" in out.lower()
        assert "git pull --rebase origin main" in out
        assert "git reset --hard origin/main" in out
        # Points back at the re-run command with the session id
        assert "tusk merge 523 --session 900" in out

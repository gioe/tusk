"""Regression tests for issue #1102.

The local-<default> strand guard (`_local_default_unpushed_commits`) compared by
pure SHA reachability (`git log origin/<default>..<default>`). A commit whose
CONTENT was already published to origin under a different SHA (rebase-publish)
still appeared "unpushed" and the no-checkout fast-forward path aborted with
exit 2 — with no escape hatch when primary's tree was dirty.

These tests cover the two fixes:

1. Patch-id awareness: `_drop_patch_id_published_commits` /
   `_local_default_unpushed_commits` drop commits whose `git cherry` line is '-'
   (patch already upstream), keep the '+' ones, and degrade gracefully to the
   SHA-based list when `git cherry` fails.
2. The `--allow-diverged-default` escape hatch: the no-checkout guard proceeds
   (and pushes) past genuinely-stranded commits when the flag is set, and still
   aborts (exit 2, no push) when it is not.
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


# ---------------------------------------------------------------------------
# Patch-id filter
# ---------------------------------------------------------------------------

class TestDropPatchIdPublishedCommits:
    def test_drops_already_published_keeps_genuinely_unpushed(self, monkeypatch):
        """A '-' cherry line (patch already upstream) is dropped; '+' is kept."""
        published = "1111111111111111111111111111111111111111"
        unpushed = "2222222222222222222222222222222222222222"
        commits = [("1111111", "Rebase-published orphan"),
                   ("2222222", "[TASK-9999] Genuinely unpushed")]

        def _run(args, check=True):
            if args[:2] == ["git", "cherry"]:
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout=f"- {published}\n+ {unpushed}\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _run)
        kept = tusk_merge._drop_patch_id_published_commits(commits, "main")
        assert kept == [("2222222", "[TASK-9999] Genuinely unpushed")]

    def test_cherry_failure_degrades_to_input(self, monkeypatch):
        """git cherry returncode != 0 → return the input unchanged (fail safe)."""
        commits = [("1111111", "Rebase-published orphan")]

        def _run(args, check=True):
            if args[:2] == ["git", "cherry"]:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="fatal: bad revision"
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _run)
        kept = tusk_merge._drop_patch_id_published_commits(commits, "main")
        assert kept == commits

    def test_empty_input_short_circuits(self, monkeypatch):
        called = {"cherry": False}

        def _run(args, check=True):
            if args[:2] == ["git", "cherry"]:
                called["cherry"] = True
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _run)
        assert tusk_merge._drop_patch_id_published_commits([], "main") == []
        assert called["cherry"] is False

    def test_merge_commit_unmatched_is_kept(self, monkeypatch):
        """git cherry skips merge commits; an unmatched commit stays flagged."""
        commits = [("deadbee", "Merge branch 'x'")]

        def _run(args, check=True):
            if args[:2] == ["git", "cherry"]:
                # No line for deadbee at all (cherry skipped it).
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout="- 9999999999999999999999999999999999999999\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _run)
        kept = tusk_merge._drop_patch_id_published_commits(commits, "main")
        assert kept == commits


class TestLocalDefaultUnpushedCommitsPatchId:
    def test_rebase_published_commit_not_reported(self, monkeypatch):
        """End-to-end: a local-main commit whose patch is on origin (new SHA) is
        filtered out, so the strand guard sees an empty set."""
        full = "abcdef1234567890abcdef1234567890abcdef12"

        def _run(args, check=True):
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return subprocess.CompletedProcess(args, 0, stdout=f"{full}\n", stderr="")
            if args[:2] == ["git", "log"] and "--format=%h %s" in args:
                return subprocess.CompletedProcess(
                    args, 0, stdout="abcdef1 Rebase-published orphan\n", stderr=""
                )
            if args[:2] == ["git", "cherry"]:
                # '-' → patch already on origin under a different SHA.
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"- {full}\n", stderr=""
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _run)
        assert tusk_merge._local_default_unpushed_commits("main") == []


# ---------------------------------------------------------------------------
# --allow-diverged-default escape hatch
# ---------------------------------------------------------------------------

def _complete_kwargs(branch, *, allow_diverged_default):
    return dict(
        branch_name=branch,
        default_branch="main",
        task_id=672,
        session_id=900,
        tusk_bin="/usr/bin/true",
        db_path="/tmp/does-not-matter.db",
        session_was_closed=True,
        did_stash=False,
        use_rebase=True,
        allow_diverged_default=allow_diverged_default,
    )


def _mock_run_factory(*, branch, record):
    """Mock ``run`` for the no-checkout path with one genuinely-unpushed commit.

    `git cherry` reports the unpushed commit as '+', so the patch-id filter keeps
    it — the guard then sees a real strand candidate.
    """
    full = "5a3d74c000000000000000000000000000000000"

    def _run(args, check=True):
        record.append(list(args))
        if args[:3] == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["git", "checkout"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "rebase", "origin/main"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["git", "rev-list"] and any(
            isinstance(a, str) and a == f"origin/main..{branch}" for a in args
        ):
            return subprocess.CompletedProcess(args, 0, stdout="1\n", stderr="")
        if (
            args[:3] == ["git", "rev-parse", "--verify"]
            and len(args) == 4
            and args[3] == "refs/remotes/origin/main"
        ):
            return subprocess.CompletedProcess(args, 0, stdout=f"{full}\n", stderr="")
        if (
            args[:2] == ["git", "log"]
            and "--format=%h %s" in args
            and any(a == "refs/remotes/origin/main..main" for a in args)
        ):
            return subprocess.CompletedProcess(
                args, 0, stdout="5a3d74c Upgrade tusk to v1036\n", stderr=""
            )
        if args[:2] == ["git", "cherry"]:
            # '+' → genuinely unpushed, survives the patch-id filter.
            return subprocess.CompletedProcess(args, 0, stdout=f"+ {full}\n", stderr="")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _run


def _push_calls(record):
    return [
        c for c in record
        if c[:2] == ["git", "push"]
        and any(isinstance(a, str) and a.endswith(":main") for a in c)
    ]


class TestAllowDivergedDefaultEscapeHatch:
    def test_aborts_without_flag(self, monkeypatch):
        """Default behavior: a genuinely-stranded commit still aborts with exit 2."""
        branch = "feature/TASK-672-x"
        record = []
        monkeypatch.setattr(
            tusk_merge, "run", _mock_run_factory(branch=branch, record=record)
        )
        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge._complete_no_checkout_fast_forward(
                **_complete_kwargs(branch, allow_diverged_default=False)
            )
        assert rc == 2, f"expected abort exit 2, got {rc}\n{stderr_buf.getvalue()}"
        assert "stranded" in stderr_buf.getvalue().lower()
        assert not _push_calls(record), "push must not run when the guard aborts"

    def test_proceeds_with_flag(self, monkeypatch):
        """--allow-diverged-default proceeds past the guard and pushes."""
        branch = "feature/TASK-672-x"
        record = []
        monkeypatch.setattr(
            tusk_merge, "run", _mock_run_factory(branch=branch, record=record)
        )
        # Bound the happy path: stub finalization beyond the push.
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        monkeypatch.setattr(
            tusk_merge, "_delete_remote_feature_branch_if_tracking", lambda b: None
        )
        monkeypatch.setattr(tusk_merge, "_warn_branch_auto_stash", lambda tid: None)
        monkeypatch.setattr(tusk_merge, "_resolve_merge_base", lambda *a: "base_sha")
        monkeypatch.setattr(tusk_merge, "_resolve_local_ref_sha", lambda r: "tip_sha")
        monkeypatch.setattr(tusk_merge, "_close_completed_task", lambda *a, **k: 0)
        monkeypatch.setattr(tusk_merge, "_maybe_refresh_deployed_bin", lambda *a, **k: False)
        monkeypatch.setattr(tusk_merge, "_maybe_advise_stale_deployed_bin", lambda *a, **k: None)
        monkeypatch.setattr(tusk_merge, "_cleanup_no_checkout_workspace", lambda *a, **k: True)
        monkeypatch.setattr(
            tusk_merge, "_reconcile_duplicate_task_workspaces", lambda *a, **k: True
        )

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge._complete_no_checkout_fast_forward(
                **_complete_kwargs(branch, allow_diverged_default=True)
            )
        assert rc == 0, f"expected success, got {rc}\n{stderr_buf.getvalue()}"
        assert _push_calls(record), "flag must allow the push to proceed"
        out = stderr_buf.getvalue()
        assert "--allow-diverged-default" in out
        # The stranded commit is named so the operator knows what to reconcile.
        assert "5a3d74c" in out and "Upgrade tusk to v1036" in out

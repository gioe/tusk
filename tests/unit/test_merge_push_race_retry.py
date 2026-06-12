"""Unit tests for the bounded push-race retry in the no-checkout path (issue #1072).

When tusk merge --rebase loses the no-checkout fast-forward push race to a
concurrent default-branch advance (cannot-lock-ref / fetch-first rejection),
the push is retried after a fresh fetch + rebase, up to _PUSH_RACE_MAX_RETRIES
times. Rebase conflicts surface immediately; non-rebase mode never retries.
"""

import importlib.util
import os
import subprocess
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")

RACE_STDERR = (
    "! [remote rejected] feature/TASK-42-x -> main "
    "(cannot lock ref 'refs/heads/main': is at 86c0b54 but expected 56970dd)"
)


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.get_connection = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    tusk_loader_mock.load.return_value = db_lib_mock
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


class TestIsPushRaceRejection:
    @pytest.mark.parametrize(
        "stderr",
        [
            RACE_STDERR,
            "! [rejected] main -> main (fetch first)",
            "! [rejected] main -> main (non-fast-forward)",
            "! [rejected] main -> main (stale info)",
        ],
    )
    def test_race_class_detected(self, stderr):
        mod = _load_module()
        assert mod._is_push_race_rejection(stderr) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: Authentication failed for 'https://github.com/x/y.git'",
            "fatal: unable to access 'https://github.com/x/y.git': Could not resolve host",
            "remote: error: hook declined to update refs/heads/main",
            "",
        ],
    )
    def test_non_race_failures_excluded(self, stderr):
        mod = _load_module()
        assert mod._is_push_race_rejection(stderr) is False


class _Harness:
    """Drive _complete_no_checkout_fast_forward with scripted git results."""

    def __init__(self, mod, push_results, rebase_results=None):
        self.mod = mod
        self.push_results = list(push_results)
        self.rebase_results = list(rebase_results or [])
        self.push_calls = 0
        self.rebase_calls = 0
        self.fetch_calls = 0

    def run(self, args, check=True):
        if args[:2] == ["git", "push"]:
            self.push_calls += 1
            return self.push_results.pop(0)
        if args[:2] == ["git", "fetch"]:
            self.fetch_calls += 1
            return _cp(0)
        if args[:2] == ["git", "rebase"]:
            self.rebase_calls += 1
            if self.rebase_results:
                return self.rebase_results.pop(0)
            return _cp(0)
        # checkout, merge-base --is-ancestor, everything else: succeed
        return _cp(0)

    def invoke(self, use_rebase=True):
        mod = self.mod
        merge_base_calls = []

        def fake_resolve_merge_base(branch, default):
            merge_base_calls.append((branch, default))
            return f"base-{len(merge_base_calls)}"

        self.merge_base_calls = merge_base_calls
        close_kwargs = {}

        def fake_close(tusk_bin, task_id, db_path, session_was_closed, **kwargs):
            close_kwargs.update(kwargs)
            return 0

        self.close_kwargs = close_kwargs

        with ExitStack() as stack:
            for name, value in [
                ("run", self.run),
                ("_origin_already_contains", lambda *a, **k: False),
                ("_local_default_unpushed_commits", lambda *a, **k: None),
                ("_resolve_merge_base", fake_resolve_merge_base),
                ("_resolve_local_ref_sha", lambda ref: "deadbeef"),
                ("_delete_remote_feature_branch_if_tracking", lambda *a, **k: None),
                ("_warn_branch_auto_stash", lambda *a, **k: None),
                ("_try_pop_stash", lambda *a, **k: None),
                ("checkpoint_wal", lambda *a, **k: None),
                ("_close_completed_task", fake_close),
                ("_maybe_refresh_deployed_bin", lambda *a, **k: False),
                ("_maybe_advise_stale_deployed_bin", lambda *a, **k: None),
                ("_cleanup_no_checkout_workspace", lambda *a, **k: True),
                ("_reconcile_duplicate_task_workspaces", lambda *a, **k: True),
                ("_recover_version_changelog_rebase_conflict", lambda *a, **k: False),
            ]:
                stack.enter_context(patch.object(mod, name, value))
            return mod._complete_no_checkout_fast_forward(
                branch_name="feature/TASK-42-x",
                default_branch="main",
                task_id=42,
                session_id=7,
                tusk_bin="tusk",
                db_path="db",
                session_was_closed=True,
                did_stash=False,
                use_rebase=use_rebase,
            )


class TestPushRaceRetry:
    def test_retry_succeeds_after_race_loss(self, capsys):
        mod = _load_module()
        h = _Harness(mod, push_results=[_cp(1, stderr=RACE_STDERR), _cp(0)])
        rc = h.invoke(use_rebase=True)

        captured = capsys.readouterr()
        assert rc == 0
        assert h.push_calls == 2
        # Initial rebase + one retry rebase.
        assert h.rebase_calls == 2
        assert "lost a race to a concurrent advance (attempt 1/3)" in captured.err
        # merge-base recomputed after the retry rebase (pre-push + retry).
        assert len(h.merge_base_calls) == 2
        assert h.close_kwargs["merge_base_sha"] == "base-2"

    def test_retries_exhausted_surfaces_error(self, capsys):
        mod = _load_module()
        h = _Harness(
            mod,
            push_results=[
                _cp(1, stderr=RACE_STDERR),
                _cp(1, stderr=RACE_STDERR),
                _cp(1, stderr=RACE_STDERR),
            ],
        )
        rc = h.invoke(use_rebase=True)

        captured = capsys.readouterr()
        assert rc == 2
        # 1 initial attempt + _PUSH_RACE_MAX_RETRIES retries.
        assert h.push_calls == 1 + mod._PUSH_RACE_MAX_RETRIES
        assert "no-checkout fast-forward push failed" in captured.err

    def test_non_retryable_failure_no_retry(self, capsys):
        mod = _load_module()
        h = _Harness(
            mod,
            push_results=[_cp(1, stderr="fatal: Authentication failed")],
        )
        rc = h.invoke(use_rebase=True)

        captured = capsys.readouterr()
        assert rc == 2
        assert h.push_calls == 1
        assert "lost a race" not in captured.err

    def test_non_rebase_mode_never_retries(self, capsys):
        mod = _load_module()
        h = _Harness(mod, push_results=[_cp(1, stderr=RACE_STDERR)])
        rc = h.invoke(use_rebase=False)

        captured = capsys.readouterr()
        assert rc == 2
        assert h.push_calls == 1
        assert h.rebase_calls == 0
        assert "lost a race" not in captured.err

    def test_conflict_during_retry_surfaces_immediately(self, capsys):
        mod = _load_module()
        h = _Harness(
            mod,
            push_results=[_cp(1, stderr=RACE_STDERR)],
            # Initial rebase succeeds; the retry rebase conflicts.
            rebase_results=[_cp(0), _cp(1, stderr="CONFLICT (content): VERSION")],
        )
        rc = h.invoke(use_rebase=True)

        captured = capsys.readouterr()
        assert rc == 2
        assert h.push_calls == 1
        assert "conflicts must be resolved manually" in captured.err

"""Regression tests for PR-mode merge recovery."""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _cp(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _open_session_connection():
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (7,)
    return conn


def _patch_pr_merge_preflight(monkeypatch, mod, *, merge_sha):
    tusk_calls = []
    close_completed_calls = []
    cleanup_calls = []

    monkeypatch.setattr(mod, "_resolve_stable_tusk_bin", lambda db_path, fallback: "/repo/bin/tusk")
    monkeypatch.setattr(mod, "get_connection", lambda db_path: _open_session_connection())
    monkeypatch.setattr(mod, "load_merge_mode", lambda path: "local")
    monkeypatch.setattr(
        mod,
        "_recorded_task_workspace",
        lambda *args: {
            "branch": "feature/TASK-42-pr",
            "workspace_path": "/tmp/task-42-pr",
        },
    )
    monkeypatch.setattr(mod, "detect_default_branch", lambda: "main")
    monkeypatch.setattr(mod, "_branch_exists", lambda branch: True)
    monkeypatch.setattr(mod, "_branch_has_task_commits", lambda branch, task_id, default: True)
    monkeypatch.setattr(mod.os.path, "exists", lambda path: True)
    monkeypatch.setattr(mod.os.path, "realpath", lambda path: path)
    monkeypatch.setattr(mod.os, "getcwd", lambda: "/tmp/task-42-pr")
    monkeypatch.setattr(mod, "_guard_no_open_completion_criteria", lambda *args: 0)
    monkeypatch.setattr(mod, "_run_pre_merge_lint", lambda *args, **kwargs: 0)
    monkeypatch.setattr(mod, "_emit_spec_drift_advisory", lambda *args: None)
    monkeypatch.setattr(mod, "_resolve_merge_commit_sha_pr", lambda pr_number: merge_sha)
    monkeypatch.setattr(mod, "checkpoint_wal", lambda db_path: None)

    def fake_run(args, check=True):
        if args[:4] == ["gh", "pr", "merge", "1183"]:
            return _cp(
                1,
                stderr=(
                    "failed to run git: fatal: 'main' is already used by "
                    "worktree at '/repo'"
                ),
            )
        if args[:2] == ["git", "branch"]:
            return _cp(0)
        return _cp(0)

    def fake_tusk_subcommand(tusk_bin, args):
        tusk_calls.append(args)
        return _cp(0)

    def fake_close_completed_task(
        tusk_bin,
        task_id,
        db_path,
        session_was_closed,
        *,
        merge_commit_sha=None,
        merge_base_sha=None,
    ):
        close_completed_calls.append(
            {
                "task_id": task_id,
                "session_was_closed": session_was_closed,
                "merge_commit_sha": merge_commit_sha,
                "merge_base_sha": merge_base_sha,
            }
        )
        return 0

    def fake_remove_recorded_task_worktree(db_path, task_id, branch_name, workspace=None):
        cleanup_calls.append(
            {
                "task_id": task_id,
                "branch_name": branch_name,
                "workspace": workspace,
            }
        )
        return True

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "_run_tusk_subcommand", fake_tusk_subcommand)
    monkeypatch.setattr(mod, "_close_completed_task", fake_close_completed_task)
    monkeypatch.setattr(mod, "_maybe_refresh_deployed_bin", lambda *args: None)
    monkeypatch.setattr(mod, "_remove_recorded_task_worktree", fake_remove_recorded_task_worktree)

    return tusk_calls, close_completed_calls, cleanup_calls


def test_pr_merge_recovers_when_gh_failed_after_remote_merge(monkeypatch, tmp_path):
    mod = _load_module()
    db = tmp_path / "tasks.db"
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    merge_sha = "701c902ae9abbba63af931ba3102ba3af1dbbf45"
    tusk_calls, close_completed_calls, cleanup_calls = _patch_pr_merge_preflight(
        monkeypatch,
        mod,
        merge_sha=merge_sha,
    )

    rc = mod.main([str(db), str(config), "42", "--session", "7", "--pr", "--pr-number", "1183"])

    assert rc == 0
    assert ["session-close", "7"] in tusk_calls
    assert close_completed_calls == [
        {
            "task_id": 42,
            "session_was_closed": True,
            "merge_commit_sha": merge_sha,
            "merge_base_sha": None,
        }
    ]
    assert cleanup_calls == [
        {
            "task_id": 42,
            "branch_name": "feature/TASK-42-pr",
            "workspace": {
                "branch": "feature/TASK-42-pr",
                "workspace_path": "/tmp/task-42-pr",
            },
        }
    ]


def test_pr_merge_failure_without_merged_pr_does_not_finalize(monkeypatch, tmp_path):
    mod = _load_module()
    db = tmp_path / "tasks.db"
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    tusk_calls, close_completed_calls, cleanup_calls = _patch_pr_merge_preflight(
        monkeypatch,
        mod,
        merge_sha=None,
    )

    rc = mod.main([str(db), str(config), "42", "--session", "7", "--pr", "--pr-number", "1183"])

    assert rc == 2
    assert tusk_calls == []
    assert close_completed_calls == []
    assert cleanup_calls == []

"""Regression tests for merge session close timing."""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
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


def test_ff_merge_failure_leaves_explicit_session_open(monkeypatch, tmp_path):
    mod = _load_module()
    db = tmp_path / "tasks.db"
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    tusk_calls = []

    monkeypatch.setattr(mod, "_resolve_stable_tusk_bin", lambda db_path, fallback: "/repo/bin/tusk")
    monkeypatch.setattr(mod, "get_connection", lambda db_path: _open_session_connection())
    monkeypatch.setattr(mod, "load_merge_mode", lambda path: "local")
    monkeypatch.setattr(mod, "_recorded_task_workspace", lambda *args: None)
    monkeypatch.setattr(mod, "find_task_branch", lambda task_id: ("feature/TASK-42-bug", None, False))
    monkeypatch.setattr(mod, "_guard_no_open_completion_criteria", lambda *args: 0)
    monkeypatch.setattr(mod, "_run_pre_merge_lint", lambda *args, **kwargs: 0)
    monkeypatch.setattr(mod, "detect_default_branch", lambda: "main")
    monkeypatch.setattr(mod, "_has_remote", lambda: False)
    monkeypatch.setattr(mod, "_worktree_path_for_branch", lambda branch: None)
    monkeypatch.setattr(mod, "_resolve_merge_base", lambda branch, default: "base-sha")
    monkeypatch.setattr(mod, "_try_pop_stash", lambda task_id: None)

    def fake_tusk_subcommand(tusk_bin, args):
        tusk_calls.append(args)
        return _cp(0)

    def fake_run(args, check=True):
        if args[:2] == ["git", "diff"]:
            return _cp(0, stdout="")
        if args == ["git", "checkout", "main"]:
            return _cp(0)
        if args[:2] == ["git", "rev-list"]:
            return _cp(0, stdout="1\n")
        if args[:2] == ["git", "log"]:
            return _cp(0, stdout="abc123 [TASK-42] fix\n")
        if args[:2] == ["git", "cherry"]:
            return _cp(0, stdout="+ abc123\n")
        if args[:3] == ["git", "merge", "--ff-only"]:
            return _cp(128, stderr="fatal: Not possible to fast-forward, aborting.")
        return _cp(0)

    monkeypatch.setattr(mod, "_run_tusk_subcommand", fake_tusk_subcommand)
    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "_run_with_index_lock_retry", lambda args, label: fake_run(args))
    monkeypatch.setattr(mod, "checkpoint_wal", lambda db_path: None)

    rc = mod.main([str(db), str(config), "42", "--session", "7"])

    assert rc == 2
    assert ["session-close", "7"] not in tusk_calls

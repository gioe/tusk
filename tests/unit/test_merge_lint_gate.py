"""Unit tests for the required pre-merge lint gate."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_pre_merge_lint_blocks_on_nonzero(monkeypatch, tmp_path, capsys):
    mod = _load_module()
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, 1, stdout="Rule 16\n  WARN - violation\n", stderr=""
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_pre_merge_lint("/repo/bin/tusk", str(config), 42, cwd="/worktree")

    assert rc == 6
    assert calls[0][0] == ["/repo/bin/tusk", "lint", "--quiet", "--task", "42"]
    assert calls[0][1]["cwd"] == "/worktree"
    assert "aborting merge" in capsys.readouterr().err


def test_pre_merge_lint_allows_zero_with_advisory(monkeypatch, tmp_path, capsys):
    mod = _load_module()
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="=== Summary: no blocking violations (1 advisory) ===\n", stderr=""
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_pre_merge_lint("/repo/bin/tusk", str(config), 42)

    assert rc == 0
    assert "no blocking violations" in capsys.readouterr().err


def test_pre_merge_lint_timeout_blocks(monkeypatch, tmp_path, capsys):
    mod = _load_module()
    config = tmp_path / "config.json"
    config.write_text('{"lint_timeout_sec": 5}', encoding="utf-8")

    def fake_run(args, **kwargs):
        trace_file = Path(kwargs["env"]["TUSK_LINT_TRACE_FILE"])
        trace_file.write_text("Rule 99: slow rule\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_pre_merge_lint("/repo/bin/tusk", str(config), 42)

    err = capsys.readouterr().err
    assert rc == 8
    assert "timed out after 5s" in err
    assert "Rule 99: slow rule" in err


def test_pre_merge_lint_skip_lint_bypasses_subprocess(monkeypatch, tmp_path, capsys):
    mod = _load_module()
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("skip_lint must not spawn tusk lint")

    monkeypatch.setattr(mod.subprocess, "run", fail_if_called)

    rc = mod._run_pre_merge_lint(
        "/repo/bin/tusk", str(config), 42, cwd="/worktree", skip_lint=True
    )

    assert rc == 0
    assert "Skipping pre-merge lint (--skip-lint)." in capsys.readouterr().err


def test_main_aborts_before_session_close_when_lint_fails(monkeypatch, tmp_path):
    mod = _load_module()
    db = tmp_path / "tasks.db"
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(mod, "_resolve_stable_tusk_bin", lambda db_path, fallback: "/repo/bin/tusk")
    monkeypatch.setattr(mod, "_autodetect_session", lambda *args: (7, None))
    monkeypatch.setattr(mod, "load_merge_mode", lambda path: "local")
    monkeypatch.setattr(mod, "_recorded_task_workspace", lambda *args: None)
    monkeypatch.setattr(
        mod, "find_task_branch", lambda task_id: ("feature/TASK-42-lint", None, False)
    )
    monkeypatch.setattr(mod, "_run_pre_merge_lint", lambda *args, **kwargs: 6)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("merge must abort before mutating session or git state")

    monkeypatch.setattr(mod, "_run_tusk_subcommand", fail_if_called)
    monkeypatch.setattr(mod, "run", fail_if_called)

    rc = mod.main([str(db), str(config), "42"])

    assert rc == 6


@pytest.mark.parametrize("flag", ["--skip-lint", "--skip-verify"])
def test_main_accepts_pre_merge_bypass_flags(monkeypatch, tmp_path, flag):
    mod = _load_module()
    db = tmp_path / "tasks.db"
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    calls = []

    monkeypatch.setattr(mod, "_resolve_stable_tusk_bin", lambda db_path, fallback: "/repo/bin/tusk")
    monkeypatch.setattr(mod, "_autodetect_session", lambda *args: (7, None))
    monkeypatch.setattr(mod, "load_merge_mode", lambda path: "local")
    monkeypatch.setattr(mod, "_recorded_task_workspace", lambda *args: None)
    monkeypatch.setattr(
        mod, "find_task_branch", lambda task_id: ("feature/TASK-42-lint", None, False)
    )

    def fake_lint(*args, **kwargs):
        calls.append((args, kwargs))
        return 6

    monkeypatch.setattr(mod, "_run_pre_merge_lint", fake_lint)

    rc = mod.main([str(db), str(config), "42", flag])

    assert rc == 6
    assert calls[0][1]["skip_lint"] is True

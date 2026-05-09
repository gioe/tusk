"""Regression tests for criteria verification in linked worktrees."""

import importlib.util
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_criteria",
    os.path.join(REPO_ROOT, "bin", "tusk-criteria.py"),
)
criteria_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(criteria_mod)


def test_verification_rewrites_relative_venv_python_from_primary_checkout(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    primary = tmp_path / "primary"
    interpreter = primary / "apps" / "scraper" / ".venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")

    original = "cd apps/scraper && .venv/bin/python3 -m pytest -q"
    expected = f"cd apps/scraper && {interpreter} -m pytest -q"
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if (
            isinstance(args, list)
            and args[:3] == ["git", "rev-parse", "--path-format=absolute"]
            and kw.get("cwd") == str(worktree)
        ):
            if args[3] == "--git-dir":
                return subprocess.CompletedProcess(args, 0, stdout="/tmp/worktree/.git\n", stderr="")
            if args[3] == "--git-common-dir":
                return subprocess.CompletedProcess(args, 0, stdout=f"{primary}/.git\n", stderr="")
        if kw.get("shell") and args == original:
            return subprocess.CompletedProcess(
                args,
                127,
                stdout="",
                stderr="/bin/sh: .venv/bin/python3: No such file or directory\n",
            )
        if kw.get("shell") and args.endswith(expected):
            return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(criteria_mod, "_get_repo_root", lambda: str(worktree))
    monkeypatch.setattr(criteria_mod.subprocess, "run", fake_run)

    result = criteria_mod.run_verification("test", original)

    assert result == {"passed": True, "output": "ok"}

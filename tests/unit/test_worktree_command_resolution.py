"""Unit tests for linked-worktree command rewriting."""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-worktree-command.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_worktree_command", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cp(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def test_cd_relative_venv_python_rewrites_to_primary_checkout_interpreter():
    mod = _load_module()
    calls: list[list[str]] = []

    def fake_run(args, check=True, cwd=None, **_kwargs):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "--path-format=absolute"]:
            if args[3] == "--git-dir":
                return _cp(0, stdout="/tmp/worktree/.git\n")
            if args[3] == "--git-common-dir":
                return _cp(0, stdout="/repo/.git\n")
        return _cp(1)

    def fake_exists(path):
        return path == "/repo/apps/scraper/.venv/bin/python3"

    command = "cd apps/scraper && .venv/bin/python3 -m pytest -q"
    rewritten, did_rewrite = mod.rewrite_linked_worktree_venv_command(
        command,
        "/tmp/worktree",
        runner=fake_run,
        exists=fake_exists,
    )

    assert did_rewrite is True
    assert rewritten == "cd apps/scraper && /repo/apps/scraper/.venv/bin/python3 -m pytest -q"


def test_repo_relative_venv_python_rewrites_without_cd_prefix():
    mod = _load_module()

    def fake_run(args, check=True, cwd=None, **_kwargs):
        if args[:3] == ["git", "rev-parse", "--path-format=absolute"]:
            if args[3] == "--git-dir":
                return _cp(0, stdout="/tmp/worktree/.git\n")
            if args[3] == "--git-common-dir":
                return _cp(0, stdout="/repo/.git\n")
        return _cp(1)

    rewritten, did_rewrite = mod.rewrite_linked_worktree_venv_command(
        "apps/scraper/.venv/bin/python -m pytest",
        "/tmp/worktree",
        runner=fake_run,
        exists=lambda path: path == "/repo/apps/scraper/.venv/bin/python",
    )

    assert did_rewrite is True
    assert rewritten == "/repo/apps/scraper/.venv/bin/python -m pytest"


def test_command_is_unchanged_when_primary_checkout_interpreter_is_missing():
    mod = _load_module()

    def fake_run(args, check=True, cwd=None, **_kwargs):
        if args[:3] == ["git", "rev-parse", "--path-format=absolute"]:
            if args[3] == "--git-dir":
                return _cp(0, stdout="/tmp/worktree/.git\n")
            if args[3] == "--git-common-dir":
                return _cp(0, stdout="/repo/.git\n")
        return _cp(1)

    command = "cd apps/scraper && .venv/bin/python3 -m pytest -q"
    rewritten, did_rewrite = mod.rewrite_linked_worktree_venv_command(
        command,
        "/tmp/worktree",
        runner=fake_run,
        exists=lambda _path: False,
    )

    assert did_rewrite is False
    assert rewritten == command


def test_command_is_unchanged_in_primary_checkout():
    mod = _load_module()

    def fake_run(args, check=True, cwd=None, **_kwargs):
        if args[:3] == ["git", "rev-parse", "--path-format=absolute"]:
            return _cp(0, stdout="/repo/.git\n")
        return _cp(1)

    command = ".venv/bin/python3 -m pytest"
    rewritten, did_rewrite = mod.rewrite_linked_worktree_venv_command(
        command,
        "/repo",
        runner=fake_run,
        exists=lambda _path: True,
    )

    assert did_rewrite is False
    assert rewritten == command

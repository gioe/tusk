"""Integration tests for install.sh hook wiring (TASK-469).

Companion to tests/integration/test_git_hooks.py — that file covers the
existing guards and the dispatcher contract end-to-end; this file is scoped
to wiring assertions for newly-added guards. Adding a guard here is the
checklist item for the "is this hook actually invoked?" question.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")

MARKER = "TUSK_HOOK_DISPATCHER_V1"


def _run(cmd, cwd, check=True):
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check:
        assert result.returncode == 0, (
            f"command {cmd} failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


@pytest.fixture()
def codex_sandbox(tmp_path):
    """A codex-layout git repo with tusk installed via install.sh."""
    _run(["git", "init"], tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n")
    _run(["bash", INSTALL_SH], tmp_path)
    return tmp_path


def test_scope_guard_wired_pre_commit(codex_sandbox):
    """install.sh writes scope-guard into the pre-commit dispatcher and copies the script.

    Wiring lives in two places that both have to agree:
      1. tusk/bin/hooks/git/scope-guard.sh is copied + executable
      2. .git/hooks/pre-commit names "scope-guard" in its for-loop
    """
    guard = codex_sandbox / "tusk" / "bin" / "hooks" / "git" / "scope-guard.sh"
    assert guard.exists(), "scope-guard.sh should be installed under tusk/bin/hooks/git/"
    assert os.access(str(guard), os.X_OK), "scope-guard.sh should be executable"

    pre_commit = codex_sandbox / ".git" / "hooks" / "pre-commit"
    assert pre_commit.exists(), ".git/hooks/pre-commit dispatcher should be installed"
    body = pre_commit.read_text()
    assert MARKER in body, "pre-commit dispatcher should carry the tusk marker"
    # The dispatcher's `for g in ...` loop names each guard; scope-guard must be in it
    assert "scope-guard" in body, (
        "pre-commit dispatcher should list scope-guard in its guard loop"
    )

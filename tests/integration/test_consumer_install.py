"""Integration tests for consumer-mode install.sh behavior (TASK-179, issue #558).

When tusk is installed into a downstream consumer repo (i.e. install.sh runs
from a different directory than the install target), source-only hooks must
NOT be wired up:

- `auto-lint.sh` (PostToolUse Edit|Write) and `version-bump-check.sh`
  (PreToolUse Bash) path-filter on `skills/*` and `bin/*` — paths that only
  exist in the tusk source repo. Registered in a consumer they exit 0 silently
  on every invocation, falsely implying enforcement.

The pre-push git dispatcher must likewise omit `version-bump-check`, while
keeping `branch-naming` (which works in any repo).

`conventions-preflight.sh` is consumer-safe — it delegates path matching to
`tusk conventions inject` against the project's own conventions DB — and must
remain registered.
"""

import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")


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
def consumer_project(tmp_path):
    """A tmp git repo with .claude/ but no tusk source layout — pure consumer."""
    _run(["git", "init"], tmp_path)
    (tmp_path / ".claude").mkdir()
    _run(["bash", INSTALL_SH], tmp_path)
    return tmp_path


def _registered_commands(settings_path):
    """Return the set of hook command paths registered in settings.json."""
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)
    out = set()
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if cmd:
                    out.add(cmd)
    return out


def test_consumer_install_marker_records_role(consumer_project):
    """install-mode marker uses compound '<mode>-<role>' form in consumer mode."""
    marker = consumer_project / ".claude" / "bin" / "install-mode"
    assert marker.exists(), "install-mode marker must be stamped by install.sh"
    assert marker.read_text().strip() == "claude-consumer"


def test_consumer_install_skips_auto_lint_registration(consumer_project):
    """auto-lint.sh path-filters on skills/* and bin/* — useless in a consumer."""
    settings = consumer_project / ".claude" / "settings.json"
    cmds = _registered_commands(settings)
    assert not any("auto-lint.sh" in c for c in cmds), (
        f"auto-lint.sh must NOT be registered in consumer mode; saw: {cmds}"
    )


def test_consumer_install_skips_version_bump_check_registration(consumer_project):
    """version-bump-check.sh PreToolUse hook is dead weight in a consumer repo."""
    settings = consumer_project / ".claude" / "settings.json"
    cmds = _registered_commands(settings)
    assert not any("version-bump-check.sh" in c for c in cmds), (
        f"version-bump-check.sh must NOT be registered in consumer mode; saw: {cmds}"
    )


def test_consumer_install_keeps_conventions_preflight(consumer_project):
    """conventions-preflight.sh is consumer-safe and must remain registered."""
    settings = consumer_project / ".claude" / "settings.json"
    cmds = _registered_commands(settings)
    assert any("conventions-preflight.sh" in c for c in cmds), (
        f"conventions-preflight.sh must remain registered in consumer mode; saw: {cmds}"
    )


def test_consumer_pre_push_dispatcher_excludes_version_bump_check(consumer_project):
    """pre-push dispatcher must keep branch-naming and drop version-bump-check."""
    pre_push = consumer_project / ".git" / "hooks" / "pre-push"
    assert pre_push.exists(), "pre-push dispatcher must be installed"
    contents = pre_push.read_text()
    assert "branch-naming" in contents, "branch-naming must be wired into pre-push"
    assert "version-bump-check" not in contents, (
        "version-bump-check must NOT be in the pre-push dispatcher in consumer mode"
    )


def test_consumer_install_still_copies_hook_files(consumer_project):
    """Source-only hook files are still copied to .claude/hooks/ — only the
    settings.json registration is skipped. This keeps a future role transition
    (consumer → source) cheap, since reinstall just rewrites settings.json."""
    hooks_dir = consumer_project / ".claude" / "hooks"
    assert (hooks_dir / "auto-lint.sh").exists(), "hook file should still be copied"
    assert (hooks_dir / "version-bump-check.sh").exists(), "hook file should still be copied"


def test_consumer_install_logs_skipped_hooks(tmp_path):
    """install.sh must surface which hooks it skipped so operators can audit."""
    _run(["git", "init"], tmp_path)
    (tmp_path / ".claude").mkdir()
    result = subprocess.run(
        ["bash", INSTALL_SH],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert "Skipped source-only hook" in result.stdout, (
        f"install.sh should log skipped source-only hooks; stdout was:\n{result.stdout}"
    )
    assert "auto-lint.sh" in result.stdout
    assert "version-bump-check.sh" in result.stdout

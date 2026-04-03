"""Integration tests verifying install.sh propagates conventions-preflight.sh.

Tests:
1. install.sh copies conventions-preflight.sh to target .claude/hooks/
2. install.sh merges the Edit|Write PreToolUse entry into target settings.json
3. Running install.sh twice does not duplicate the hook entry
"""

import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
HOOK_NAME = "conventions-preflight.sh"


@pytest.fixture()
def target_project(tmp_path):
    """A tmp dir with a bare git repo and .claude/ — simulates a target Claude Code project."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _run_install(target_path):
    result = subprocess.run(
        ["bash", INSTALL_SH],
        cwd=str(target_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"install.sh failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return result


def _hook_entry_count(settings_path):
    """Return the number of hook groups in settings.json that register conventions-preflight.sh."""
    with open(settings_path) as f:
        settings = json.load(f)
    count = 0
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for h in group.get("hooks", []):
                if HOOK_NAME in h.get("command", ""):
                    count += 1
    return count


def test_install_copies_hook_file(target_project):
    """install.sh copies conventions-preflight.sh to target .claude/hooks/."""
    _run_install(target_project)
    hook_dest = target_project / ".claude" / "hooks" / HOOK_NAME
    assert hook_dest.exists(), f"{HOOK_NAME} was not copied to target .claude/hooks/"
    assert os.access(str(hook_dest), os.X_OK), f"{HOOK_NAME} is not executable"


def test_install_merges_settings_entry(target_project):
    """install.sh merges the Edit|Write PreToolUse entry into target settings.json."""
    _run_install(target_project)
    settings_path = target_project / ".claude" / "settings.json"
    assert settings_path.exists(), "settings.json was not created by install.sh"
    count = _hook_entry_count(settings_path)
    assert count == 1, f"Expected 1 conventions-preflight hook entry, found {count}"

    with open(settings_path) as f:
        settings = json.load(f)
    pre_hooks = settings.get("hooks", {}).get("PreToolUse", [])
    preflight_groups = [
        g for g in pre_hooks
        if any(HOOK_NAME in h.get("command", "") for h in g.get("hooks", []))
    ]
    assert len(preflight_groups) == 1
    assert preflight_groups[0]["matcher"] == "Edit|Write"


def test_install_idempotent_no_duplicate_entry(target_project):
    """Running install.sh twice does not duplicate the conventions-preflight hook entry."""
    _run_install(target_project)
    _run_install(target_project)
    settings_path = target_project / ".claude" / "settings.json"
    count = _hook_entry_count(settings_path)
    assert count == 1, (
        f"Expected 1 conventions-preflight hook entry after two installs, found {count}"
    )

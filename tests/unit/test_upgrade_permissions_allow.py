"""Unit tests for tusk-upgrade.py merge_hook_registrations permissions.allow merging.

Verifies that tusk upgrade propagates permissions.allow entries from the source
settings.json into the target settings.json without clobbering existing entries.
This covers GitHub Issue #352 where re-review agents lacked Bash access because
tusk upgrade never merged permissions.allow for existing projects.
"""

import importlib.util
import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPGRADE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-upgrade.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_upgrade", UPGRADE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_settings(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def _read_settings(path):
    return json.loads(path.read_text())


class TestMergePermissionsAllow:
    def test_new_entries_are_added(self, tmp_path):
        """permissions.allow entries from source are appended to target."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {
            "permissions": {"allow": ["Bash(git diff:*)", "Bash(tusk review:*)"]}
        })
        _write_settings(tgt_claude / "settings.json", {
            "permissions": {"allow": ["Bash(git status:*)"]}
        })

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        allow = result["permissions"]["allow"]
        assert "Bash(git diff:*)" in allow
        assert "Bash(tusk review:*)" in allow
        assert "Bash(git status:*)" in allow  # existing entry preserved

    def test_existing_entries_not_duplicated(self, tmp_path):
        """Entries already in target are not duplicated."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {
            "permissions": {"allow": ["Bash(git diff:*)", "Bash(tusk review:*)"]}
        })
        _write_settings(tgt_claude / "settings.json", {
            "permissions": {"allow": ["Bash(git diff:*)"]}
        })

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        allow = result["permissions"]["allow"]
        assert allow.count("Bash(git diff:*)") == 1
        assert "Bash(tusk review:*)" in allow

    def test_target_with_no_permissions_block(self, tmp_path):
        """Target settings.json with no permissions block gets one created."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {
            "permissions": {"allow": ["Bash(git diff:*)"]}
        })
        _write_settings(tgt_claude / "settings.json", {"hooks": {}})

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        assert result["permissions"]["allow"] == ["Bash(git diff:*)"]

    def test_source_with_no_permissions_block(self, tmp_path):
        """Source settings.json with no permissions block leaves target unchanged."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {"hooks": {}})
        _write_settings(tgt_claude / "settings.json", {
            "permissions": {"allow": ["Bash(git status:*)"]}
        })

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        assert result["permissions"]["allow"] == ["Bash(git status:*)"]

    def test_missing_target_settings_created_from_source(self, tmp_path):
        """When target settings.json does not exist, it is created with source entries."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {
            "permissions": {"allow": ["Bash(git diff:*)"]}
        })
        # No target settings.json

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        assert "Bash(git diff:*)" in result["permissions"]["allow"]

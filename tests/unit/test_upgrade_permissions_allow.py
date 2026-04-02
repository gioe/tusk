"""Unit tests for tusk-upgrade.py merge_hook_registrations permissions.allow merging.

Verifies that tusk upgrade propagates permissions.allow entries from the source
settings.json into the target settings.json without clobbering existing entries.
This covers GitHub Issue #352 where re-review agents lacked Bash access because
tusk upgrade never merged permissions.allow for existing projects.
"""

import importlib.util
import json
import os

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


class TestCheckReviewCommitsPermissions:
    def test_returns_empty_when_all_present(self, tmp_path):
        """No missing entries when all required permissions are already in settings.json."""
        mod = _load_module()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        _write_settings(claude_dir / "settings.json", {
            "permissions": {"allow": list(mod.REQUIRED_REVIEW_COMMITS_PERMISSIONS)}
        })
        missing = mod.check_review_commits_permissions(str(tmp_path))
        assert missing == []

    def test_returns_missing_entries(self, tmp_path):
        """Returns only the entries that are absent from settings.json."""
        mod = _load_module()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        _write_settings(claude_dir / "settings.json", {
            "permissions": {"allow": ["Bash(git diff:*)"]}
        })
        missing = mod.check_review_commits_permissions(str(tmp_path))
        assert "Bash(git diff:*)" not in missing
        assert "Bash(tusk review:*)" in missing
        assert "Bash(git remote:*)" in missing

    def test_returns_all_when_no_settings_file(self, tmp_path):
        """All required entries reported missing when settings.json does not exist."""
        mod = _load_module()
        (tmp_path / ".claude").mkdir()
        missing = mod.check_review_commits_permissions(str(tmp_path))
        assert set(missing) == set(mod.REQUIRED_REVIEW_COMMITS_PERMISSIONS)

    def test_returns_all_when_settings_malformed(self, tmp_path):
        """All required entries reported missing when settings.json is not valid JSON."""
        mod = _load_module()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("not valid json")
        missing = mod.check_review_commits_permissions(str(tmp_path))
        assert set(missing) == set(mod.REQUIRED_REVIEW_COMMITS_PERMISSIONS)

    def test_returns_all_when_no_permissions_block(self, tmp_path):
        """All required entries reported missing when permissions block is absent."""
        mod = _load_module()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        _write_settings(claude_dir / "settings.json", {"hooks": {}})
        missing = mod.check_review_commits_permissions(str(tmp_path))
        assert set(missing) == set(mod.REQUIRED_REVIEW_COMMITS_PERMISSIONS)

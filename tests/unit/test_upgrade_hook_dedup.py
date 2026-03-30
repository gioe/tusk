"""Unit tests for merge_hook_registrations path-normalization dedup fix.

Verifies that hooks registered with relative paths (e.g. .claude/hooks/foo.sh)
are correctly detected as duplicates of $CLAUDE_PROJECT_DIR-prefixed equivalents
written by the current install, preventing accumulation on repeated upgrades.
This covers GitHub Issue #421.
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


class TestNormalizeHookCmd:
    def test_strips_claude_project_dir_prefix(self):
        mod = _load_module()
        assert mod._normalize_hook_cmd(
            "$CLAUDE_PROJECT_DIR/.claude/hooks/foo.sh"
        ) == ".claude/hooks/foo.sh"

    def test_strips_leading_dot_slash(self):
        mod = _load_module()
        assert mod._normalize_hook_cmd("./.claude/hooks/foo.sh") == ".claude/hooks/foo.sh"

    def test_plain_path_unchanged(self):
        mod = _load_module()
        assert mod._normalize_hook_cmd(".claude/hooks/foo.sh") == ".claude/hooks/foo.sh"

    def test_empty_string_unchanged(self):
        mod = _load_module()
        assert mod._normalize_hook_cmd("") == ""

    def test_strips_git_root_resolved_wrapper(self):
        mod = _load_module()
        cmd = "bash -c 'R=$(git rev-parse --show-toplevel 2>/dev/null); [ -z \"$R\" ] && exit 0; exec \"$R/.claude/hooks/block-raw-sqlite.sh\"'"
        assert mod._normalize_hook_cmd(cmd) == ".claude/hooks/block-raw-sqlite.sh"

    def test_strips_git_root_resolved_wrapper_different_hook(self):
        mod = _load_module()
        cmd = "bash -c 'R=$(git rev-parse --show-toplevel 2>/dev/null); [ -z \"$R\" ] && exit 0; exec \"$R/.claude/hooks/auto-lint.sh\"'"
        assert mod._normalize_hook_cmd(cmd) == ".claude/hooks/auto-lint.sh"


class TestMergeHookDedup:
    def test_relative_path_not_duplicated_by_prefixed_source(self, tmp_path):
        """Existing relative-path hook is not duplicated when source uses $CLAUDE_PROJECT_DIR prefix."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [
                        {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/block-raw-sqlite.sh"}
                    ]}
                ]
            }
        })
        _write_settings(tgt_claude / "settings.json", {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [
                        {"type": "command", "command": ".claude/hooks/block-raw-sqlite.sh"}
                    ]}
                ]
            }
        })

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        groups = result["hooks"]["PreToolUse"]
        assert len(groups) == 1, f"Expected 1 hook group, got {len(groups)} — duplicate was added"

    def test_dot_slash_path_not_duplicated_by_prefixed_source(self, tmp_path):
        """Existing ./  path hook is not duplicated when source uses $CLAUDE_PROJECT_DIR prefix."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {
            "hooks": {"PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/post-write.sh"}]}
            ]}
        })
        _write_settings(tgt_claude / "settings.json", {
            "hooks": {"PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "./.claude/hooks/post-write.sh"}]}
            ]}
        })

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        groups = result["hooks"]["PostToolUse"]
        assert len(groups) == 1, f"Expected 1 hook group, got {len(groups)} — duplicate was added"

    def test_repeated_merge_does_not_accumulate(self, tmp_path):
        """Calling merge_hook_registrations twice does not add a second copy."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        src_settings = {
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/block-raw-sqlite.sh"}]}
            ]}
        }
        tgt_settings = {
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": ".claude/hooks/block-raw-sqlite.sh"}]}
            ]}
        }
        _write_settings(src_claude / "settings.json", src_settings)
        _write_settings(tgt_claude / "settings.json", tgt_settings)

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))
        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        groups = result["hooks"]["PreToolUse"]
        assert len(groups) == 1, f"Expected 1 hook group after 2 merges, got {len(groups)}"

    def test_duplicate_print_shows_normalized_path(self, tmp_path, capsys):
        """'Hook already registered' message prints the normalized (canonical) path form."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/block-raw-sqlite.sh"}
                ]}
            ]}
        })
        _write_settings(tgt_claude / "settings.json", {
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": ".claude/hooks/block-raw-sqlite.sh"}
                ]}
            ]}
        })

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        captured = capsys.readouterr()
        assert "Hook already registered: .claude/hooks/block-raw-sqlite.sh" in captured.out
        assert "$CLAUDE_PROJECT_DIR" not in captured.out

    def test_git_root_resolved_hook_not_duplicated_by_plain_source(self, tmp_path):
        """Existing git-root-resolved hook is not duplicated when source uses plain path."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        git_root_cmd = (
            "bash -c 'R=$(git rev-parse --show-toplevel 2>/dev/null); "
            "[ -z \"$R\" ] && exit 0; exec \"$R/.claude/hooks/block-raw-sqlite.sh\"'"
        )
        _write_settings(src_claude / "settings.json", {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [
                        {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/block-raw-sqlite.sh"}
                    ]}
                ]
            }
        })
        _write_settings(tgt_claude / "settings.json", {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [
                        {"type": "command", "command": git_root_cmd}
                    ]}
                ]
            }
        })

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        groups = result["hooks"]["PreToolUse"]
        assert len(groups) == 1, f"Expected 1 hook group, got {len(groups)} — git-root hook was duplicated"

    def test_genuinely_new_hook_is_added(self, tmp_path):
        """A new hook that is not present in target is still added."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude = tmp_path / "tgt" / ".claude"
        tgt_claude.mkdir(parents=True)

        _write_settings(src_claude / "settings.json", {
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/new-hook.sh"}]}
            ]}
        })
        _write_settings(tgt_claude / "settings.json", {
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": ".claude/hooks/existing-hook.sh"}]}
            ]}
        })

        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        result = _read_settings(tgt_claude / "settings.json")
        groups = result["hooks"]["PreToolUse"]
        assert len(groups) == 2, f"Expected 2 hook groups (existing + new), got {len(groups)}"

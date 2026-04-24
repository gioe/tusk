"""Unit tests for tusk-upgrade.py Codex-mode helpers (TASK-136).

Covers the two pure helpers added for dual-target support:

- detect_install_mode(script_dir) — reads the install-mode marker file
- translate_manifest_for_mode(files, mode) — rewrites tarball MANIFEST paths
  for the local install layout (claude pass-through, codex rewrites .claude/bin/
  to tusk/bin/ and drops .claude/skills/, .claude/hooks/ entries)
"""

import importlib.util
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPGRADE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-upgrade.py")


@pytest.fixture(scope="module")
def upgrade_mod():
    spec = importlib.util.spec_from_file_location("tusk_upgrade", UPGRADE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDetectInstallMode:
    def test_absent_marker_defaults_to_claude(self, tmp_path, upgrade_mod):
        assert upgrade_mod.detect_install_mode(str(tmp_path)) == "claude"

    def test_codex_marker(self, tmp_path, upgrade_mod):
        (tmp_path / "install-mode").write_text("codex\n")
        assert upgrade_mod.detect_install_mode(str(tmp_path)) == "codex"

    def test_claude_marker(self, tmp_path, upgrade_mod):
        (tmp_path / "install-mode").write_text("claude\n")
        assert upgrade_mod.detect_install_mode(str(tmp_path)) == "claude"

    def test_marker_whitespace_tolerated(self, tmp_path, upgrade_mod):
        (tmp_path / "install-mode").write_text("  codex  \n\n")
        assert upgrade_mod.detect_install_mode(str(tmp_path)) == "codex"

    def test_unknown_value_falls_back_to_claude(self, tmp_path, upgrade_mod):
        (tmp_path / "install-mode").write_text("gemini\n")
        assert upgrade_mod.detect_install_mode(str(tmp_path)) == "claude"


class TestTranslateManifestForMode:
    SAMPLE = [
        ".claude/bin/tusk",
        ".claude/bin/tusk-commit.py",
        ".claude/bin/config.default.json",
        ".claude/skills/tusk/SKILL.md",
        ".claude/hooks/setup-path.sh",
    ]

    def test_claude_mode_is_passthrough(self, upgrade_mod):
        assert upgrade_mod.translate_manifest_for_mode(self.SAMPLE, "claude") == self.SAMPLE

    def test_codex_mode_rewrites_bin_and_drops_skills_and_hooks(self, upgrade_mod):
        result = upgrade_mod.translate_manifest_for_mode(self.SAMPLE, "codex")
        assert result == [
            "tusk/bin/tusk",
            "tusk/bin/tusk-commit.py",
            "tusk/bin/config.default.json",
        ]

    def test_empty_list(self, upgrade_mod):
        assert upgrade_mod.translate_manifest_for_mode([], "codex") == []

    def test_codex_mode_leaves_non_claude_paths_intact(self, upgrade_mod):
        # If a future tarball includes files outside .claude/, they should pass
        # through unchanged rather than being dropped silently.
        files = [".claude/bin/tusk", "scripts/extra.py", "tusk/config.json"]
        assert upgrade_mod.translate_manifest_for_mode(files, "codex") == [
            "tusk/bin/tusk",
            "scripts/extra.py",
            "tusk/config.json",
        ]

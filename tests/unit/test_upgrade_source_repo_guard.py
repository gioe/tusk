"""Unit tests for the source-repo guard in tusk-upgrade.py (TASK-151).

Running `tusk upgrade` inside the tusk source repo used to crash in
stage_and_commit because `git add --force` chokes on the .claude/skills/*
symlinks created by `tusk sync-skills`. `is_source_repo()` detects the source
layout via three concrete markers (skills/ real dir with SKILL.md, install.sh
at repo root, .claude/skills/* symlinks) so main() can exit cleanly before any
filesystem work happens.
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


def _make_source_layout(root):
    """Create a minimal tusk source-repo layout: skills/foo/SKILL.md + install.sh +
    .claude/skills/foo symlinked to ../../skills/foo."""
    (root / "skills" / "foo").mkdir(parents=True)
    (root / "skills" / "foo" / "SKILL.md").write_text("# foo\n")
    (root / "install.sh").write_text("#!/bin/bash\nexit 0\n")
    (root / ".claude" / "skills").mkdir(parents=True)
    os.symlink("../../skills/foo", str(root / ".claude" / "skills" / "foo"))


def _make_target_layout(root):
    """Create a minimal installed-project layout: .claude/skills/foo is a real dir
    and there is no skills/ sibling or install.sh at repo root."""
    (root / ".claude" / "skills" / "foo").mkdir(parents=True)
    (root / ".claude" / "skills" / "foo" / "SKILL.md").write_text("# foo\n")


class TestIsSourceRepo:
    def test_detects_source_layout(self, tmp_path, upgrade_mod):
        _make_source_layout(tmp_path)
        assert upgrade_mod.is_source_repo(str(tmp_path)) is True

    def test_rejects_target_layout(self, tmp_path, upgrade_mod):
        _make_target_layout(tmp_path)
        assert upgrade_mod.is_source_repo(str(tmp_path)) is False

    def test_rejects_empty_dir(self, tmp_path, upgrade_mod):
        assert upgrade_mod.is_source_repo(str(tmp_path)) is False

    def test_rejects_missing_install_sh(self, tmp_path, upgrade_mod):
        _make_source_layout(tmp_path)
        (tmp_path / "install.sh").unlink()
        assert upgrade_mod.is_source_repo(str(tmp_path)) is False

    def test_rejects_skills_without_skill_md(self, tmp_path, upgrade_mod):
        _make_source_layout(tmp_path)
        (tmp_path / "skills" / "foo" / "SKILL.md").unlink()
        assert upgrade_mod.is_source_repo(str(tmp_path)) is False

    def test_rejects_real_dir_claude_skills(self, tmp_path, upgrade_mod):
        # Source-repo-ish layout but .claude/skills/foo is a real directory
        # instead of a symlink — this is what an installed project looks like,
        # and should never be mistaken for the source.
        (tmp_path / "skills" / "foo").mkdir(parents=True)
        (tmp_path / "skills" / "foo" / "SKILL.md").write_text("# foo\n")
        (tmp_path / "install.sh").write_text("#!/bin/bash\nexit 0\n")
        (tmp_path / ".claude" / "skills" / "foo").mkdir(parents=True)
        (tmp_path / ".claude" / "skills" / "foo" / "SKILL.md").write_text("# foo\n")
        assert upgrade_mod.is_source_repo(str(tmp_path)) is False

    def test_rejects_empty_claude_skills(self, tmp_path, upgrade_mod):
        (tmp_path / "skills" / "foo").mkdir(parents=True)
        (tmp_path / "skills" / "foo" / "SKILL.md").write_text("# foo\n")
        (tmp_path / "install.sh").write_text("#!/bin/bash\nexit 0\n")
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        assert upgrade_mod.is_source_repo(str(tmp_path)) is False

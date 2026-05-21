"""Documentation-surface checks for /ios-libs-contribute.

The skill is distributed as repo content rather than executable code, so the
regression surface is file presence plus the workflow invariants that keep the
upstream PR linked to the originating tusk task.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "skills" / "ios-libs-contribute" / "SKILL.md"
CODEX_PROMPT = REPO_ROOT / "codex-prompts" / "ios-libs-contribute.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestIosLibsContributeSkill:
    def test_skill_file_exists_with_ios_app_frontmatter(self):
        text = _text(SKILL)

        assert "name: ios-libs-contribute" in text
        assert "applies_to_project_types: [ios_app]" in text
        assert "description:" in text

    def test_skill_resolves_configured_lib_repo_without_hard_coded_repo(self):
        text = _text(SKILL)

        assert "project_libs" in text
        assert "PROJECT_TYPE" in text
        assert "LIB_REPO" in text
        assert "gioe/ios-libs" not in text

    def test_skill_documents_fork_based_pr_workflow(self):
        text = _text(SKILL)

        required_fragments = [
            "mktemp -d",
            'cd "$LIB_WORKSPACE_PARENT"',
            "gh repo fork",
            "tusk/<task_id>-<slug>",
            "copy",
            "test suite",
            "git commit",
            "git push",
            "gh pr create",
            "tusk progress",
            "Originating tusk task",
        ]
        for fragment in required_fragments:
            assert fragment in text

    def test_codex_prompt_port_exists_and_preserves_core_workflow(self):
        text = _text(CODEX_PROMPT)

        assert "# iOS Libs Contribute" in text
        assert "project_libs" in text
        assert "gh pr create" in text
        assert "tusk progress" in text

    def test_agent_guides_list_skill(self):
        for path in (REPO_ROOT / "CLAUDE.md", REPO_ROOT / "AGENTS.md"):
            text = _text(path)
            assert "**`/ios-libs-contribute`**" in text


def test_distribution_version_and_changelog_are_bumped_for_skill_delivery():
    """The test guards a single invariant: every VERSION bump must be matched by
    a CHANGELOG entry for that exact version. Self-validates from the VERSION
    file so adding new entries never breaks this test (issue #802 — the prior
    form pinned 30+ historic CHANGELOG lines which silently broke on every
    subsequent bump and was unrelated to skill delivery).
    """
    version = _text(REPO_ROOT / "VERSION").strip()
    changelog = _text(REPO_ROOT / "CHANGELOG.md")

    assert version.isdigit() and int(version) > 0, (
        f"VERSION must be a positive integer; got {version!r}"
    )
    expected_heading = f"## [{version}] -"
    assert expected_heading in changelog, (
        f"CHANGELOG.md is missing a `{expected_heading} <date>` entry for the "
        f"current VERSION ({version}). Run `tusk changelog-add <task_id>` from "
        "the worktree to add it (the version is sourced from the VERSION file "
        "automatically; see TASK-398)."
    )

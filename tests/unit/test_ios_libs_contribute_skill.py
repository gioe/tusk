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
    version = _text(REPO_ROOT / "VERSION").strip()
    changelog = _text(REPO_ROOT / "CHANGELOG.md")

    assert version == "890"
    assert "## [890] - 2026-05-09" in changelog
    assert "[TASK-365] Add task-worktree prune command" in changelog
    assert "## [889] - 2026-05-09" in changelog
    assert "[TASK-363] Fix tusk branch when the default branch is checked out in another worktree" in changelog
    assert "## [888] - 2026-05-08" in changelog
    assert "[TASK-361] Base task worktrees on freshly fetched origin default branches" in changelog
    assert "## [887] - 2026-05-08" in changelog
    assert "[TASK-360] Rebase tusk merge --rebase onto origin/default" in changelog
    assert "## [886] - 2026-05-08" in changelog
    assert "[TASK-358] Preflight tusk merge worktree-lock failures before closing sessions" in changelog
    assert "[TASK-359] Let tusk merge accept worktree-TASK branch fallbacks" in changelog
    assert "## [885] - 2026-05-08" in changelog
    assert "[TASK-352] Make merge and abandon clean up task-owned worktrees" in changelog
    assert "## [884] - 2026-05-08" in changelog
    assert "[TASK-353] Wire /tusk and /chain to use task worktrees by default" in changelog
    assert "## [883] - 2026-05-08" in changelog
    assert "[TASK-351] Add task-owned worktree create/list commands" in changelog
    assert "## [882] - 2026-05-08" in changelog
    assert "[TASK-355] Fix full-suite regressions after task workspace schema work" in changelog
    assert "## [881] - 2026-05-08" in changelog
    assert "[TASK-342] Extract shared tusk-branch auto-stash parsing into git helpers" in changelog
    assert "## [880] - 2026-05-08" in changelog
    assert "[TASK-349] Fix review begin to prefer current remote default over stale local default" in changelog
    assert "## [879] - 2026-05-08" in changelog
    assert "[TASK-348] Fix skill-run finish to report diagnostics for silent nonzero failures" in changelog
    assert "## [878] - 2026-05-08" in changelog
    assert "[TASK-347] Fix node -e verification spec quoting and supersede regenerated broken criteria" in changelog
    assert "## [877] - 2026-05-07" in changelog
    assert "[TASK-346] Fix: tusk merge handles default branch checked out in another worktree" in changelog
    assert "## [876] - 2026-05-07" in changelog
    assert "[TASK-345] Fix review begin diff inference when only origin/default is usable in worktrees" in changelog
    assert "## [875] - 2026-05-07" in changelog
    assert "[TASK-257] Ship /ios-libs-contribute skill" in changelog

"""Regression tests for Codex-safe task worktree root guidance."""

import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(relpath: str) -> str:
    with open(os.path.join(REPO_ROOT, relpath), encoding="utf-8") as handle:
        return handle.read()


def _preflight(text: str) -> str:
    start = text.index("**Writable-root preflight (before the first create):**")
    normal_create = text.index(
        "tusk task-worktree create <id> <brief-description-slug>\n", start
    )
    return " ".join(text[start:normal_create].split())


def test_tusk_workflows_preflight_managed_writable_roots_before_create():
    for relpath in ("skills/tusk/SKILL.md", "codex-prompts/tusk.md"):
        block = _preflight(_read(relpath))

        assert "TUSK_WORKTREE_ROOT" in block
        assert "authorized writable filesystem roots" in block
        assert "~/.tusk/worktrees" in block
        assert "test -w" in block
        assert "--workspace-root <authorized-root>/tusk-worktrees" in block
        assert "outside the primary checkout" in block
        assert "never create an inaccessible worktree and then relocate it" in block
        assert "per-repository namespace" in block


def test_tusk_workflows_forbid_hardcoded_platform_fallback():
    for relpath in ("skills/tusk/SKILL.md", "codex-prompts/tusk.md"):
        block = _preflight(_read(relpath))

        assert "Never hardcode `/private/tmp`" in block
        assert block.count("/private/tmp") == 1

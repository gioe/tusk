"""Regression tests for retro scope-quality guidance."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_empty_scope_guidance_respects_scope_enforced_commits():
    text = (REPO_ROOT / "skills" / "retro" / "SKILL.md").read_text(encoding="utf-8")

    assert "first check `task.scope_enforced`" in text
    assert "do **not** infer a guard bypass from `scope list` alone" in text
    assert "TUSK_SCOPE_GUARD_BYPASS=1" in text

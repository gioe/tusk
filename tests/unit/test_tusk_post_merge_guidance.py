"""Regression tests for /tusk post-merge verification guidance."""

import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(relpath: str) -> str:
    with open(os.path.join(REPO_ROOT, relpath), encoding="utf-8") as handle:
        return handle.read()


def test_tusk_skill_documents_post_merge_verification_deferral():
    text = _read("skills/tusk/SKILL.md")

    assert "Post-merge verification criteria" in text
    assert "tusk criteria skip <criterion_id>" in text
    assert "post-merge verification:" in text
    assert "refuses ordinary open, non-deferred criteria" in text


def test_codex_tusk_prompt_documents_post_merge_verification_deferral():
    text = _read("codex-prompts/tusk.md")

    assert "Post-merge verification criteria" in text
    assert "tusk criteria skip <criterion_id>" in text
    assert "post-merge verification:" in text
    assert "refuses ordinary open, non-deferred criteria" in text

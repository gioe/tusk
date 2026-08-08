"""Regression coverage for source release metadata workflow guidance."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ("skills/tusk/SKILL.md", "codex-prompts/tusk.md")
HEADING = "10b. **Prepare source-repository release metadata before final review.**"


@pytest.mark.parametrize("relative_path", WORKFLOWS)
def test_release_checkpoint_precedes_review_and_preserves_role_routing(relative_path):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    start = text.index(HEADING)
    review = text.index("11. **Run", start)
    block = " ".join(text[start:review].split())

    assert start < review
    assert "install-mode" in block
    assert "-consumer" in block
    assert "-source" in block
    assert "legacy plain marker" in block
    assert "missing marker" in block
    assert "Do not infer the role from CWD" in block


@pytest.mark.parametrize("relative_path", WORKFLOWS)
def test_release_checkpoint_commits_one_bump_before_review(relative_path):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    start = text.index(HEADING)
    review = text.index("11. **Run", start)
    block = " ".join(text[start:review].split())

    assert "hooks/git/version-bump-check.sh" in block
    assert "tusk scope add <id> VERSION" in block
    assert "tusk scope add <id> CHANGELOG.md" in block
    assert "tusk version-bump" in block
    assert "tusk changelog-add <id>" in block
    assert "tusk commit <id>" in block
    assert "committed before Step 11" in block
    assert "do **not** bump VERSION again" in block
    assert "one release bump" in block


@pytest.mark.parametrize("relative_path", WORKFLOWS)
def test_release_checkpoint_preserves_chain_consolidation(relative_path):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    start = text.index(HEADING)
    review = text.index("11. **Run", start)
    block = " ".join(text[start:review].split())

    assert "When `/chain` owns the run, skip it" in block
    assert "consolidates one VERSION and CHANGELOG update" in block

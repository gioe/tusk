from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_review_commit_guidance_routes_spec_gaps_to_durable_artifacts():
    for relpath in (
        "skills/review-commits/SKILL.md",
        "codex-prompts/review-commits.md",
    ):
        text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        assert "--spec-gap-type" in text
        assert "missing_criterion" in text
        assert "missing_verification" in text
        assert "tusk criteria add" in text
        assert "tusk context add" in text
        assert "follow-up task" in text

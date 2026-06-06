"""Regression coverage for review context-atom routing guidance."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills" / "review-commits" / "SKILL.md"
CODEX_PROMPT_PATH = REPO_ROOT / "codex-prompts" / "review-commits.md"


def _bodies():
    return (
        SKILL_PATH.read_text(encoding="utf-8"),
        CODEX_PROMPT_PATH.read_text(encoding="utf-8"),
    )


def test_review_suggestions_can_be_preserved_as_context_atoms():
    for body in _bodies():
        normalized = " ".join(body.split())

        assert "between four branches" in normalized
        assert "Preserve as a context atom" in body
        assert "--source review --type decision" in body
        assert "--source review --type assumption" in body
        assert "--source review --type risk" in body
        assert "--source review --type question" in body
        assert "--source review --type memory" in body
        assert "Do not write directly to `task_context_items`" in body or (
            "Do **not** write directly to `task_context_items`" in body
        )


def test_review_dismissals_preserve_durable_resolution_context():
    for body in _bodies():
        normalized = " ".join(body.split())

        assert "durable design reason" in normalized
        assert "context item ID in the dismissal note" in normalized
        assert "preserved as <type> context atom #<context_item_id>" in normalized


def test_review_final_summary_audits_preserved_context_atoms():
    for body in _bodies():
        normalized = " ".join(body.split())

        assert "context:   <review_source_count> atoms preserved from review" in body
        assert "source='review'" in normalized
        assert "audit cue for review decisions preserved outside the backlog" in normalized

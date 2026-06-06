"""Regression coverage for /tusk startup context hydration guidance."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills" / "tusk" / "SKILL.md"
CODEX_PROMPT_PATH = REPO_ROOT / "codex-prompts" / "tusk.md"


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _codex_prompt_text() -> str:
    return CODEX_PROMPT_PATH.read_text(encoding="utf-8")


def test_tusk_workflow_invokes_task_brief_after_task_start():
    for body in (_skill_text(), _codex_prompt_text()):
        normalized = " ".join(body.split())

        assert "tusk task-brief <id>" in body
        assert "compiled brief" in normalized
        assert "after `tusk task-start" in normalized
        assert "before code exploration" in normalized


def test_tusk_workflow_classifies_mode_and_validates_context_health():
    for body in (_skill_text(), _codex_prompt_text()):
        normalized = " ".join(body.split())

        assert "Classify the task mode" in body
        assert "bug fix" in normalized
        assert "feature" in normalized
        assert "test-only" in normalized
        assert "docs-only" in normalized
        assert "context_health_warnings" in body
        assert "scope" in normalized
        assert "blocking open questions" in normalized


def test_tusk_workflow_treats_criteria_as_plan_and_scope_as_contract():
    for body in (_skill_text(), _codex_prompt_text()):
        normalized = " ".join(body.split())

        assert "incomplete criteria are the execution plan" in normalized
        assert "scope is a contract" in normalized
        assert "Do not begin implementation while blocking open questions remain" in normalized

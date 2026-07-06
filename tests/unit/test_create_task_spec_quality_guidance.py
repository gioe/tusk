"""Regression coverage for spec-quality /create-task guidance."""

import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "create-task", "SKILL.md")
CODEX_PROMPT_PATH = os.path.join(REPO_ROOT, "codex-prompts", "create-task.md")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _assert_spec_quality_guidance(body: str) -> None:
    lower = body.lower()
    assert "**WHAT**" in body
    assert "**WHY**" in body
    assert "**HOW**" in body
    assert "verification hint" in lower
    assert "when the source material provides" in lower
    assert "enough information" in lower
    assert "--typed-criteria" in body
    assert "tusk context add <task_id> --source create_task --type assumption" in body
    assert "tusk context add <task_id> --source create_task --type risk" in body
    assert "tusk context add <task_id> --source create_task --type decision" in body
    assert "instead of burying" in lower


def test_claude_create_task_skill_pins_spec_quality_guidance():
    _assert_spec_quality_guidance(_read(SKILL_PATH))


def test_codex_create_task_prompt_pins_spec_quality_guidance():
    _assert_spec_quality_guidance(_read(CODEX_PROMPT_PATH))

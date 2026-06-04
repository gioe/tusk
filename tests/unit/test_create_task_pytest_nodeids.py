"""Regression coverage for /create-task pytest node-id guidance."""

import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "create-task", "SKILL.md")
CODEX_PROMPT_PATH = os.path.join(REPO_ROOT, "codex-prompts", "create-task.md")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_create_task_skill_requires_class_prefixed_pytest_nodeids():
    body = _read(SKILL_PATH)

    assert "::TestClassName::test_method_name" in body
    assert "::test_method_name" in body
    assert "not found" in body


def test_codex_prompt_requires_class_prefixed_pytest_nodeids():
    body = _read(CODEX_PROMPT_PATH)

    assert "::TestClassName::test_method_name" in body
    assert "::test_method_name" in body
    assert "not found" in body

"""Regression coverage for task-import documentation and creation guidance."""

import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENTS_PATH = os.path.join(REPO_ROOT, "AGENTS.md")
SCRIPTS_PATH = os.path.join(REPO_ROOT, "docs", "SCRIPTS.md")
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "create-task", "SKILL.md")
CODEX_PROMPT_PATH = os.path.join(REPO_ROOT, "codex-prompts", "create-task.md")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_agents_lists_task_import_file_and_stdin_usage():
    body = _read(AGENTS_PATH)

    assert "bin/tusk task-import --file tasks.json [--dry-run] [--best-effort]" in body
    assert "bin/tusk task-import --stdin --dry-run" in body
    assert "Prefer task-import over repeated task-insert calls" in body


def test_scripts_document_task_import_contract():
    body = _read(SCRIPTS_PATH)

    assert "#### `tusk task-import` JSON contract" in body
    assert "Top-level input must be an object with a `tasks` array" in body
    assert "`created`, `skipped`, and `failed` maps" in body
    assert "`--dry-run`" in body
    assert "`--best-effort`" in body
    assert "`duplicate_policy`" in body
    assert "`depends_on`" in body
    assert "`objectives`" in body


def _assert_create_task_prefers_import_for_multi_task_materialization(body: str) -> None:
    assert "When two or more tasks are approved" in body
    assert "tusk task-import --stdin --dry-run" in body
    assert "not repeated `tusk task-insert` calls" in body
    assert "local `key` values and `depends_on`" in body
    assert "`created`, `skipped`, and `failed`" in body


def test_claude_create_task_skill_prefers_import_for_multiple_tasks():
    _assert_create_task_prefers_import_for_multi_task_materialization(_read(SKILL_PATH))


def test_codex_create_task_prompt_prefers_import_for_multiple_tasks():
    _assert_create_task_prefers_import_for_multi_task_materialization(_read(CODEX_PROMPT_PATH))

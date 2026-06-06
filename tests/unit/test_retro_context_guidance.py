"""Regression coverage for retro context-atom routing guidance."""

import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(relative_path):
    with open(os.path.join(REPO_ROOT, relative_path), encoding="utf-8") as f:
        return f.read()


def test_lightweight_retro_routes_to_smallest_durable_unit():
    body = _read("skills/retro/SKILL.md")

    assert "smallest durable unit" in body
    assert "**Task**" in body
    assert "**Criterion**" in body
    assert "**Context atom**" in body
    assert 'tusk criteria add <task_id> "<criterion>"' in body
    assert "tusk context add <task_id>" in body
    assert "tusk context resolve <context_item_id>" in body
    assert "tusk context supersede <context_item_id>" in body
    assert "Do **not** use direct SQL for context atoms" in body
    assert "Context atoms must not inflate the task backlog" in body


def test_full_retro_consumes_context_health_and_uses_context_cli():
    body = _read("skills/retro/FULL-RETRO.md")

    assert "**`context_health`**" in body
    assert "task_context_items" in body
    assert "Do **not** issue separate SQL against `task_context_items`" in body
    assert "Context Snapshot" in body
    assert "Durable memory" in body
    assert "task, a criterion on an existing task, or a context atom" in body
    assert "tusk context add <task_id>" in body
    assert "tusk context resolve <context_item_id>" in body
    assert "tusk context supersede <context_item_id>" in body
    assert "Promote to tasks only when the item requires a shippable change" in body


def test_codex_retro_prompt_mirrors_context_atom_routing():
    body = _read("codex-prompts/retro.md")

    assert "`context_health`" in body
    assert "smallest durable unit" in body
    assert "**Context atom**" in body
    assert "tusk criteria add <task_id>" in body
    assert "tusk context add <task_id>" in body
    assert "tusk context resolve <context_item_id>" in body
    assert "tusk context supersede <context_item_id>" in body
    assert "Do not query or update\n`task_context_items` directly" in body
    assert "Context atoms updated" in body

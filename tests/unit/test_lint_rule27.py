"""Unit tests for rule27_task_worktree_prompt_drift in tusk-lint.py."""

import importlib.util
import os
import tempfile


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint",
    os.path.join(REPO_ROOT, "bin", "tusk-lint.py"),
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def _write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_rule27_flags_branch_first_task_prompts():
    with tempfile.TemporaryDirectory() as tmp:
        _write(
            tmp,
            "skills/tusk/SKILL.md",
            "2. **Create a new git branch IMMEDIATELY**\n"
            "```bash\n"
            "tusk branch <id> <brief-description-slug>\n"
            "```\n",
        )
        _write(
            tmp,
            "codex-prompts/tusk.md",
            "2. **Create a new git branch IMMEDIATELY**\n"
            "```bash\n"
            "tusk branch <id> <brief-description-slug>\n"
            "```\n",
        )
        _write(
            tmp,
            "skills/chain/AGENT-PROMPT.md",
            "git checkout -b feature/TASK-{id}-<brief-slug>\n",
        )
        _write(
            tmp,
            "codex-prompts/chain.md",
            "Follow tusk.md Step 1 onward for that task ID -- start, branch,\n",
        )

        violations = lint.rule27_task_worktree_prompt_drift(tmp)

    assert len(violations) >= 4
    assert any("skills/tusk/SKILL.md" in v for v in violations)
    assert any("codex-prompts/tusk.md" in v for v in violations)
    assert any("skills/chain/AGENT-PROMPT.md" in v for v in violations)
    assert any("codex-prompts/chain.md" in v for v in violations)


def test_rule27_allows_task_worktree_flow():
    with tempfile.TemporaryDirectory() as tmp:
        _write(
            tmp,
            "skills/tusk/SKILL.md",
            "2. **Create or reuse the task-owned workspace**\n"
            "```bash\n"
            "tusk task-worktree create <id> <brief-description-slug>\n"
            "```\n",
        )
        _write(
            tmp,
            "codex-prompts/tusk.md",
            "2. **Create or reuse the task-owned workspace**\n"
            "```bash\n"
            "tusk task-worktree create <id> <brief-description-slug>\n"
            "```\n",
        )
        _write(
            tmp,
            "skills/chain/AGENT-PROMPT.md",
            "tusk task-worktree create {id} <brief-slug>\n",
        )
        _write(
            tmp,
            "codex-prompts/chain.md",
            "Follow tusk.md Step 1 onward -- start, create or reuse its task-owned workspace.\n",
        )

        assert lint.rule27_task_worktree_prompt_drift(tmp) == []

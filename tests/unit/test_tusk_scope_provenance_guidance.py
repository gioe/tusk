"""Regression coverage for /tusk scope provenance guidance."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATHS = (
    REPO_ROOT / "skills" / "tusk" / "SKILL.md",
    REPO_ROOT / "codex-prompts" / "tusk.md",
)


def _scope_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("5b. **Declare scope before the first commit.")
    end = text.index("6. **Route implementation after delegated exploration.", start)
    return " ".join(text[start:end].split())


def test_operator_declared_uses_the_first_durable_checkpoint_boundary():
    for path in WORKFLOW_PATHS:
        block = _scope_block(path)

        assert "Reserve `operator_declared` for scope supplied during task creation or added before the task's first durable checkpoint" in block
        assert "`task-start` alone does not cross this provenance boundary" in block
        assert "first progress checkpoint or committed criterion" in block


def test_pre_checkpoint_scope_remains_operator_declared_after_start():
    for path in WORKFLOW_PATHS:
        block = _scope_block(path)

        assert "If the task has no progress checkpoint and no committed criterion" in block
        assert "implicit source is `operator_declared`" in block
        assert "even though Step 1 has already started the task" in block


def test_post_checkpoint_scope_and_unbounded_recovery_use_automatic_provenance():
    for path in WORKFLOW_PATHS:
        block = _scope_block(path)

        assert "Once a progress checkpoint or committed criterion exists" in block
        assert "records `expanded_mid_task`" in block
        assert 'tusk scope add <id> "**" --reason "..."' in block
        assert "same checkpoint-based provenance as any other addition" in block

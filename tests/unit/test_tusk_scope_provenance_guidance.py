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


def test_operator_declared_is_limited_to_pre_start_scope():
    for path in WORKFLOW_PATHS:
        block = _scope_block(path)

        assert "Reserve `operator_declared` for scope supplied during task creation or added before `task-start`" in block
        assert "already past that provenance boundary" in block
        assert "--source operator_declared" not in block


def test_post_start_scope_is_expanded_before_other_work_evidence():
    for path in WORKFLOW_PATHS:
        block = _scope_block(path)

        assert "For every missing path after task start" in block
        assert "implicit source is `expanded_mid_task`" in block
        assert "even when the path was part of the up-front plan" in block
        assert "no edits, progress checkpoints, criteria completions, or commits exist yet" in block


def test_unbounded_recovery_does_not_relabel_post_start_scope():
    for path in WORKFLOW_PATHS:
        block = _scope_block(path)

        assert 'tusk scope add <id> "**" --reason "..."' in block
        assert "records the post-start expansion honestly" in block

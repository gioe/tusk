"""Regression coverage for visual versus logic failure evidence guidance."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATHS = (
    REPO_ROOT / "skills" / "tusk" / "SKILL.md",
    REPO_ROOT / "codex-prompts" / "tusk.md",
)


def _confirm_failure_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("4. **Confirm failure using relevant evidence**")
    end = text.index("5. **Explore", start)
    return " ".join(text[start:end].split())


def test_visual_failure_evidence_is_valid_in_both_tusk_workflows():
    for path in WORKFLOW_PATHS:
        block = _confirm_failure_block(path)

        assert "current screenshot or manual visual check" in block
        assert "active build/checkout" in block
        assert "passing logic test that does not assert rendering must not cancel" in block
        assert "directly asserts the reported rendering defect" in block
        assert "screenshot, golden, pixel, or rendering assertion" in block


def test_relevant_logic_reproducer_retains_early_stop_in_both_workflows():
    for path in WORKFLOW_PATHS:
        block = _confirm_failure_block(path)

        assert "If a relevant reproducer test passes" in block
        assert "time/date sensitivity" in block
        assert "tusk skill-run cancel <run_id>" in block
        assert "stop before investigating further" in block

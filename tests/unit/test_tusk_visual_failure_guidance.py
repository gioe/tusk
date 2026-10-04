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


def test_passing_baseline_can_route_to_red_regression_in_both_workflows():
    for path in WORKFLOW_PATHS:
        block = _confirm_failure_block(path)

        baseline_index = block.index("A named test or suite is a baseline")
        red_test_index = block.index("add and run a focused regression test")
        cancel_index = block.index("tusk skill-run cancel <run_id>")

        assert "unless its assertions directly exercise the reported failure" in block
        assert "acceptance criteria require new regression coverage" in block
        assert "expected pre-fix red result confirms the failure" in block
        assert "Do not cancel based only on a passing baseline" in block
        assert baseline_index < red_test_index < cancel_index


def test_direct_logic_reproducer_retains_early_stop_in_both_workflows():
    for path in WORKFLOW_PATHS:
        block = _confirm_failure_block(path)

        assert "If a test that directly exercises the reported failure passes" in block
        assert "time/date sensitivity" in block
        assert "passing evidence directly disprove the report" in block
        assert "tusk skill-run cancel <run_id>" in block
        assert "stop before investigating further" in block


def test_empty_test_runs_are_inconclusive_before_the_pass_branch():
    for path in WORKFLOW_PATHS:
        block = _confirm_failure_block(path)
        guard_index = block.index("Verify test execution before interpreting the exit code")
        pass_index = block.index("If a test that directly exercises the reported failure passes")
        assert guard_index < pass_index
        guard = block[guard_index:pass_index]
        for signal in ("zero tests", "all-skipped", "no matching tests"):
            assert signal in guard
        assert "inconclusive" in guard
        assert "Do not cancel the skill-run or mark the task complete" in guard
        assert "at least one relevant test actually executed" in guard
        assert "inspect the selected files and test-name filter" in guard


def test_missing_regression_is_authored_without_requiring_an_issue_test():
    for path in WORKFLOW_PATHS:
        block = _confirm_failure_block(path)
        author_index = block.index("If no existing test exercises the reported behavior")
        pass_index = block.index("If a test that directly exercises the reported failure passes")
        assert author_index < pass_index
        guidance = block[author_index:pass_index]
        assert "write and run a focused regression test" in guidance
        assert "does not need to supply a failing test" in guidance
        assert "pre-fix failure" in guidance

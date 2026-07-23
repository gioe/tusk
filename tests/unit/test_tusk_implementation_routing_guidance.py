"""Regression coverage for conditional /tusk implementation routing."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATHS = (
    REPO_ROOT / "skills" / "tusk" / "SKILL.md",
    REPO_ROOT / "codex-prompts" / "tusk.md",
)


def _routing_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("5. **Explore the codebase before implementing")
    end = text.index("7. **Implement, commit, and mark criteria done", start)
    return " ".join(text[start:end].split())


def test_tusk_workflows_always_delegate_exploration_before_routing():
    for path in WORKFLOW_PATHS:
        block = _routing_block(path)

        assert "always delegate this exploration pass to a sub-agent" in block.lower()
        assert "Wait for the exploration sub-agent to finish" in block
        assert "report its findings before choosing a route" in block


def test_tusk_workflows_allow_only_focused_xs_s_work_to_remain_local():
    for path in WORKFLOW_PATHS:
        block = _routing_block(path)

        assert "Local implementation is eligible only for XS/S tasks" in block
        assert "exact files and relevant tests" in block
        assert "focused and unambiguous" in block
        assert "Delegate implementation for M/L/XL tasks" in block
        assert "broad, ambiguous, or missing exact files or tests" in block


def test_tusk_workflows_honor_explicit_delegation_requests():
    for path in WORKFLOW_PATHS:
        block = _routing_block(path)

        assert "Explicit operator requests override the size rule" in block
        assert "asks for delegation, agents, or parallel work" in block
        assert "even for an otherwise focused XS/S task" in block


def test_tusk_workflows_report_routing_before_implementation():
    for path in WORKFLOW_PATHS:
        block = _routing_block(path)

        report_index = block.index("Before writing any implementation code")
        local_index = block.index("Implementation routing: local")
        delegated_index = block.index("Implementation routing: delegated")
        step_seven_index = block.index("proceed to Step 7")

        assert report_index < local_index < step_seven_index
        assert report_index < delegated_index < step_seven_index


def test_codex_prompt_does_not_claim_subagents_are_unavailable():
    prompt = (REPO_ROOT / "codex-prompts" / "tusk.md").read_text(encoding="utf-8")

    assert "no sub-agent dispatch primitive" not in prompt
    assert "no parallel sub-agent primitive" not in prompt

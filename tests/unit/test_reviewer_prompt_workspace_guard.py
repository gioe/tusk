"""Regression coverage for the reviewer agent's existing-worktree contract."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "skills" / "review-commits" / "REVIEWER-PROMPT.md"


def _step_one_text() -> str:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    step_one = prompt.split("### Step 1: Fetch the Diff", 1)[1]
    return " ".join(step_one.split("### Step 2: Analyze for Issues", 1)[0].split())


def test_step_one_uses_only_the_existing_worktree_diff() -> None:
    step_one = _step_one_text()

    assert "assigned working directory" in step_one
    assert "task branch checked out" in step_one
    assert "full local history and origin refs" in step_one
    assert "Unpushed task commits are normal" in step_one
    assert "Never clone, fetch" in step_one
    assert "copy or overlay files" in step_one
    assert "reconstruct the repository or diff" in step_one
    assert "Use only the primary diff command" in step_one
    assert "TASK-commit recovery" in step_one
    assert 'report "No changes found to review." and stop' in step_one
    assert "Do not invent an alternative diff source" in step_one

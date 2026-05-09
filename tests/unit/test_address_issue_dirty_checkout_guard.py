"""Regression tests for address-issue dirty-checkout isolation (issue #700)."""

import os
import re


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "address-issue", "SKILL.md")


def _skill_text():
    with open(SKILL_PATH, encoding="utf-8") as f:
        return f.read()


def _step7_section():
    text = _skill_text()
    match = re.search(
        r"##\s+Step\s+7:.*?(?=\n##\s+Step\s+7\.5|\Z)",
        text,
        re.DOTALL,
    )
    assert match is not None, "Step 7 section must be present"
    return match.group(0)


def test_step7_requires_dirty_checkout_guard_before_delegating_to_tusk():
    """Step 7 must make dirty-checkout isolation explicit before /tusk handoff."""
    section = _step7_section()

    dirty_index = section.lower().find("dirty checkout")
    handoff_index = section.find("Read file: <base_directory>/../tusk/SKILL.md")

    assert dirty_index != -1, (
        "Step 7 must explicitly mention dirty checkout handling before task work starts"
    )
    assert handoff_index != -1, "Step 7 must still delegate to the /tusk skill"
    assert dirty_index < handoff_index, (
        "Dirty-checkout handling must appear before the /tusk handoff instructions"
    )


def test_step7_uses_task_worktree_and_forbids_branch_first_work():
    """Step 7 must require task-worktree isolation rather than direct tusk branch."""
    section = _step7_section()

    assert "tusk task-worktree create" in section, (
        "Step 7 must name task-worktree creation as the required isolation path"
    )
    assert re.search(r"do not\s+run\s+`?tusk branch", section, re.IGNORECASE), (
        "Step 7 must forbid direct tusk branch use from the current checkout"
    )

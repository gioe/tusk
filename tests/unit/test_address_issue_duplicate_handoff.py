"""Regression tests for duplicate handoff guidance in /address-issue."""

import os
import re


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADDRESS_ISSUE_SKILL = os.path.join(REPO_ROOT, "skills", "address-issue", "SKILL.md")
TUSK_SKILL = os.path.join(REPO_ROOT, "skills", "tusk", "SKILL.md")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _address_issue_step6():
    text = _read(ADDRESS_ISSUE_SKILL)
    match = re.search(
        r"##\s+Step\s+6:.*?(?=\n##\s+Step\s+7:|\Z)",
        text,
        re.DOTALL,
    )
    assert match is not None, "Step 6 section must be present"
    return match.group(0)


def test_address_issue_duplicate_handoff_branches_on_task_status():
    """In Progress duplicates must resume existing state, not start fresh /tusk."""
    section = _address_issue_step6()

    assert "In Progress" in section
    assert "/resume-task" in section
    assert "open session" in section
    assert "open skill-run" in section
    assert re.search(r"To Do.*?/tusk <id>", section, re.DOTALL), (
        "To Do duplicates should still route to normal /tusk startup"
    )


def test_tusk_duplicate_guidance_warns_against_fresh_start_for_in_progress():
    """The /tusk duplicate path should not imply every duplicate needs /tusk <id>."""
    text = _read(TUSK_SKILL)

    assert "duplicate" in text
    assert "In Progress duplicate" in text
    assert "/resume-task" in text
    assert "do not start a fresh `/tusk <id>`" in text

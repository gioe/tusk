"""Regression tests for address-issue description-file insertion guard."""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _step6_section(text: str) -> str:
    match = re.search(
        r"##\s+Step\s+6:.*?(?=\n##\s+Step\s+7:|\Z)",
        text,
        re.DOTALL,
    )
    assert match is not None, "Step 6 section must be present"
    return match.group(0)


def _assert_description_file_guard(relpath: str) -> None:
    section = _step6_section(_read(relpath))
    normalized = _normalized(section)

    guard_index = normalized.find("Before running task-insert")
    insert_index = normalized.find('task-insert "<summary>"')

    assert guard_index != -1, (
        f"{relpath} Step 6 must require a pre-insert description-file guard"
    )
    assert insert_index != -1, f"{relpath} Step 6 must still show task-insert"
    assert guard_index < insert_index, (
        f"{relpath} Step 6 must guard the description file before task-insert"
    )
    assert "missing or empty" in normalized
    assert "stop before inserting the task" in normalized
    assert "surface the failed issue-body fetch or description-file write" in normalized
    assert "fail-closed boundary" in normalized
    assert "Prefer separate tool calls" in normalized
    assert "explicit" in normalized and "set -e" in normalized
    assert "failed issue fetch or write cannot fall through" in normalized
    assert "newline-separated non-errexit shell command" in normalized


def test_skill_guards_missing_or_empty_description_file_before_insert():
    _assert_description_file_guard("skills/address-issue/SKILL.md")


def test_codex_prompt_guards_missing_or_empty_description_file_before_insert():
    _assert_description_file_guard("codex-prompts/address-issue.md")

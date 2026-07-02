"""Regression tests for address-issue post-merge finalization guidance."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_skill_uses_stable_checkout_for_post_merge_finalization():
    text = _read("skills/address-issue/SKILL.md")

    assert "ADDRESS_ISSUE_PRIMARY_CWD=$(pwd)" in text
    assert 'ADDRESS_ISSUE_TUSK_BIN="$ADDRESS_ISSUE_PRIMARY_CWD/bin/tusk"' in text
    assert 'cd "$ADDRESS_ISSUE_PRIMARY_CWD"' in text
    assert '"$ADDRESS_ISSUE_TUSK_BIN" skill-run finish <run_id>' in text
    assert '"$ADDRESS_ISSUE_TUSK_BIN" task-summary <task_id> --format markdown' in text
    assert "process creation can fail before tusk starts" in _normalized(text)


def test_codex_prompt_matches_stable_checkout_guidance():
    text = _read("codex-prompts/address-issue.md")

    assert "ADDRESS_ISSUE_PRIMARY_CWD=$(pwd)" in text
    assert 'ADDRESS_ISSUE_TUSK_BIN="$ADDRESS_ISSUE_PRIMARY_CWD/bin/tusk"' in text
    assert 'cd "$ADDRESS_ISSUE_PRIMARY_CWD"' in text
    assert '"$ADDRESS_ISSUE_TUSK_BIN" skill-run finish <run_id>' in text
    assert '"$ADDRESS_ISSUE_TUSK_BIN" task-summary <task_id> --format markdown' in text
    assert "process creation can fail before tusk starts" in _normalized(text)


def test_codex_prompt_uses_worktree_local_wrapper_after_handoff():
    text = _read("codex-prompts/address-issue.md")
    normalized = _normalized(text)

    assert "ADDRESS_ISSUE_WORKTREE_TUSK_BIN=\"./bin/tusk\"" in text
    assert "task-worktree-local Tusk wrapper" in normalized
    assert "use ADDRESS_ISSUE_WORKTREE_TUSK_BIN" in normalized
    assert "ADDRESS_ISSUE_TUSK_BIN is only for stable primary-checkout closeout commands" in normalized


def test_skill_uses_worktree_local_wrapper_after_handoff():
    text = _read("skills/address-issue/SKILL.md")
    normalized = _normalized(text)

    assert "ADDRESS_ISSUE_WORKTREE_TUSK_BIN=\"./bin/tusk\"" in text
    assert "task-worktree-local Tusk wrapper" in normalized
    assert "use ADDRESS_ISSUE_WORKTREE_TUSK_BIN" in normalized
    assert "ADDRESS_ISSUE_TUSK_BIN is only for stable primary-checkout closeout commands" in normalized

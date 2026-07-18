"""Regression tests for retro inline doc-patch persistence guidance."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_lr2a_file_patches_are_committed_after_apply():
    text = (REPO_ROOT / "skills" / "retro" / "SKILL.md").read_text(encoding="utf-8")
    lr2a = text.split("### LR-2a: Inline Convention/Doc Actions", 1)[1]
    lr2a = lr2a.split("### LR-2b:", 1)[0]

    assert "tusk commit" in lr2a
    assert "--allow-branch-mismatch" in lr2a
    assert "commit just the edited file" in lr2a
    assert "If the commit fails" in lr2a
    assert "Convention DB writes already persist atomically" in lr2a


def test_codex_retro_prompt_file_patches_are_committed_after_apply():
    text = (REPO_ROOT / "codex-prompts" / "retro.md").read_text(encoding="utf-8")
    lr2a = text.split("### LR-2a: Inline Convention / Prompt-Doc Actions", 1)[1]
    lr2a = lr2a.split("### LR-2b:", 1)[0]

    assert "tusk commit" in lr2a
    assert "--allow-branch-mismatch" in lr2a
    assert "commit just the edited file" in lr2a
    assert "If the commit fails" in lr2a
    assert "Convention DB writes already persist atomically" in lr2a


def test_full_retro_file_patches_allow_post_merge_branch_mismatch():
    text = (REPO_ROOT / "skills" / "retro" / "FULL-RETRO.md").read_text(
        encoding="utf-8"
    )
    inline_actions = text.split("### 5e: Inline Convention/Doc Actions", 1)[1]
    inline_actions = inline_actions.split("## Step 6:", 1)[0]

    assert "tusk commit" in inline_actions
    assert "--allow-branch-mismatch" in inline_actions
    assert "commit just the edited file" in inline_actions

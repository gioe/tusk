"""Regression coverage for committing review fixes before re-review."""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDANCE_PATHS = (
    REPO_ROOT / "skills" / "review-commits" / "SKILL.md",
    REPO_ROOT / "codex-prompts" / "review-commits.md",
)


def _step(text: str, number: int) -> str:
    start = text.index(f"## Step {number}:")
    end = text.index(f"## Step {number + 1}:", start)
    return text[start:end]


def test_each_re_review_commits_only_tracked_fix_files_before_diff_capture():
    for path in GUIDANCE_PATHS:
        step = _step(path.read_text(encoding="utf-8"), 8)
        normalized = " ".join(step.split())

        assert 'git add -- "${REVIEW_FIX_FILES[@]}"' in step
        assert re.search(
            r'git commit -m ".+Apply review fixes" -- '
            r'"\$\{REVIEW_FIX_FILES\[@\]\}"',
            step,
        )
        assert "REVIEW_FIX_FILES=()" in step
        assert "already staged" in normalized
        assert "untracked working-tree changes" in normalized

        commit_index = step.index("git commit -m")
        review_command = (
            "tusk review begin" if path.name == "SKILL.md" else "tusk review start"
        )
        assert commit_index < step.index(review_command, commit_index)


def test_final_review_fix_safeguard_remains_path_limited():
    for path in GUIDANCE_PATHS:
        step = _step(path.read_text(encoding="utf-8"), 9)

        assert 'git add -- "${REVIEW_FIX_FILES[@]}"' in step
        assert re.search(
            r'git commit -m ".+Apply review fixes" -- '
            r'"\$\{REVIEW_FIX_FILES\[@\]\}"',
            step,
        )
        assert "final safeguard" in step
        assert "remain" in step and "untouched" in step

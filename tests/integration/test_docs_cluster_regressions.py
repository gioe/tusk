"""Regressions for docs-cluster issue reports against shipped CLI names."""

import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK = os.path.join(REPO_ROOT, "bin", "tusk")
REVIEW_COMMITS_SKILL = os.path.join(REPO_ROOT, "skills", "review-commits", "SKILL.md")


def _run_tusk(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [TUSK, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_docs_cluster_reported_cli_names_are_dispatched(db_path):
    """Issue #747/#743: these names must not fall through to SQL fallback or argparse drift."""
    cases = [
        (("jots", "--task-id", "1"), 0, "[]"),
        (("review", "begin", "--help"), 0, "usage: tusk review begin"),
        (("review-agent-cost", "--help"), 0, "usage: tusk review-agent-cost"),
    ]

    for argv, expected_returncode, expected_text in cases:
        result = _run_tusk(*argv)
        combined = result.stdout + result.stderr
        assert result.returncode == expected_returncode, (
            f"`tusk {' '.join(argv)}` exited {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
        assert expected_text in combined
        assert "Unknown subcommand" not in combined
        assert "invalid choice" not in combined


def test_review_commits_skill_warns_about_review_note_shell_quoting():
    """Issue #748: review notes/comments need the same zsh hazard warning as commits."""
    with open(REVIEW_COMMITS_SKILL, encoding="utf-8") as f:
        text = f.read()

    assert "Avoid backticks and unescaped `$` in review notes and comments" in text
    assert "--note" in text
    assert "add-comment" in text

"""Regression tests for `tusk git-default-branch` in linked worktrees."""

import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(args, cwd):
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_repo_with_origin_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@example.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "seed.txt"], repo)
    _run(["git", "commit", "-q", "-m", "seed"], repo)
    _run(["git", "remote", "add", "origin", "https://example.invalid/tusk.git"], repo)
    _run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], repo)
    _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], repo)
    return repo


@pytest.mark.parametrize(
    ("worktree_args", "expected_branch"),
    [
        (["-b", "feature/TASK-1-default-branch"], "feature/TASK-1-default-branch"),
        (["--detach"], None),
    ],
)
def test_git_default_branch_uses_existing_origin_head_from_linked_worktree(
    tmp_path,
    worktree_args,
    expected_branch,
):
    repo = _make_repo_with_origin_head(tmp_path)
    worktree = tmp_path / "linked"
    _run(["git", "worktree", "add", "-q", *worktree_args, str(worktree), "HEAD"], repo)
    if expected_branch:
        assert _run(["git", "branch", "--show-current"], worktree).stdout.strip() == expected_branch
    else:
        assert _run(["git", "branch", "--show-current"], worktree).stdout.strip() == ""

    result = subprocess.run(
        [TUSK, "git-default-branch"],
        cwd=worktree,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "TUSK_QUIET": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "main"

"""Integration tests for the tusk-commit branch validation pre-flight (issue #794).

Regression: before the fix, `tusk commit <task_id>` ran `git commit` against whatever
branch HEAD pointed to — including `main` — without checking the task's recorded
workspace branch. The original report (TASK-2316) created `feature/TASK-2316-...`
via `git branch` (no checkout), then ran `tusk commit` from primary checkout on
`main`. The commit silently landed on `main` and `feature/TASK-2316-...` stayed
empty.

Cases covered:
- Workspace recorded + current branch matches      → commit succeeds
- Workspace recorded + current branch differs      → exit 7 with diagnostic
- No workspace + current branch is default         → exit 7 with diagnostic (#794)
- No workspace + current branch is non-default     → commit succeeds (legacy flow)
- `--allow-branch-mismatch` bypasses the refusal   → commit succeeds
"""

import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
TUSK_COMMIT_PY = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")
CONFIG_DEFAULT = os.path.join(REPO_ROOT, "config.default.json")


def _git(args, *, cwd):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return result


def _seed_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


def _init_tusk(repo):
    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return db_path, env


def _insert_task(db_path, summary="branch validation task"):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) VALUES "
            "(?, 'desc', 'In Progress', 'bug', 'High', 'M', 30)",
            (summary,),
        )
        conn.commit()
        return cur.lastrowid


def _record_workspace(db_path, task_id, branch, workspace_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (?, ?, ?)",
            (task_id, branch, workspace_path),
        )
        conn.commit()


def _stage_change(repo, filename="feature.txt", content="work\n"):
    target = repo / filename
    target.write_text(content, encoding="utf-8")


def _commit(repo, env, task_id, *extra_args, files=("feature.txt",), message="msg"):
    return subprocess.run(
        [
            "python3",
            TUSK_COMMIT_PY,
            str(repo),
            CONFIG_DEFAULT,
            str(task_id),
            message,
            *files,
            *extra_args,
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _setup(tmp_path):
    repo = _seed_repo(tmp_path)
    db_path, env = _init_tusk(repo)
    task_id = _insert_task(db_path)
    _stage_change(repo)
    return repo, db_path, env, task_id


def test_workspace_recorded_matching_branch_succeeds(tmp_path):
    """When current branch matches the recorded workspace branch, commit proceeds."""
    repo, db_path, env, task_id = _setup(tmp_path)
    branch = f"feature/TASK-{task_id}-validate"
    _git(["checkout", "-b", branch], cwd=repo)
    _record_workspace(db_path, task_id, branch, str(repo))

    result = _commit(repo, env, task_id, "--skip-verify")

    assert result.returncode == 0, (
        f"expected success on matching branch; got exit {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    # Confirm the commit landed on the expected branch.
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()
    assert head == branch


def test_workspace_recorded_branch_mismatch_refuses_with_diagnostic(tmp_path):
    """The issue #794 scenario: workspace recorded but operator stays on the wrong branch."""
    repo, db_path, env, task_id = _setup(tmp_path)
    expected_branch = f"feature/TASK-{task_id}-validate"
    # Create the recorded branch but DO NOT check it out — HEAD stays on main.
    _git(["branch", expected_branch], cwd=repo)
    _record_workspace(db_path, task_id, expected_branch, str(repo))

    pre_head = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    result = _commit(repo, env, task_id, "--skip-verify")

    assert result.returncode == 7, (
        f"expected exit 7 (branch mismatch); got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "current branch does not match" in result.stderr
    assert expected_branch in result.stderr
    assert "main" in result.stderr
    assert "--allow-branch-mismatch" in result.stderr
    # Critically: no commit was created on either branch.
    post_head = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    assert post_head == pre_head, "commit should NOT have landed"
    branch_head = _git(
        ["rev-parse", expected_branch], cwd=repo
    ).stdout.strip()
    assert branch_head == pre_head, "feature branch should be unchanged"


def test_no_workspace_on_default_branch_refuses(tmp_path):
    """No workspace recorded + on default branch — refuse with workspace-creation hint."""
    repo, db_path, env, task_id = _setup(tmp_path)
    # HEAD is main, no task_workspaces row inserted.

    pre_head = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    result = _commit(repo, env, task_id, "--skip-verify")

    assert result.returncode == 7, (
        f"expected exit 7 (no workspace + default branch); got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "refusing to land on the default branch 'main'" in result.stderr
    assert "tusk task-worktree create" in result.stderr
    assert "--allow-branch-mismatch" in result.stderr
    # No commit was created.
    post_head = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    assert post_head == pre_head, "commit should NOT have landed"


def test_no_workspace_on_non_default_branch_succeeds(tmp_path):
    """No workspace recorded + on a non-default branch — legacy `tusk branch` flow, allow."""
    repo, db_path, env, task_id = _setup(tmp_path)
    _git(["checkout", "-b", f"feature/TASK-{task_id}-legacy"], cwd=repo)

    result = _commit(repo, env, task_id, "--skip-verify")

    assert result.returncode == 0, (
        f"expected success on legacy feature branch; got exit {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def test_allow_branch_mismatch_bypasses_refusal(tmp_path):
    """`--allow-branch-mismatch` is the explicit escape hatch — refuse paths must pass through."""
    repo, db_path, env, task_id = _setup(tmp_path)
    expected_branch = f"feature/TASK-{task_id}-validate"
    _git(["branch", expected_branch], cwd=repo)
    _record_workspace(db_path, task_id, expected_branch, str(repo))
    # HEAD is still main — a mismatch — but the flag should bypass.

    result = _commit(
        repo, env, task_id, "--skip-verify", "--allow-branch-mismatch"
    )

    assert result.returncode == 0, (
        f"expected --allow-branch-mismatch to bypass; got exit {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    # Commit landed on main (the override path).
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()
    assert head == "main"

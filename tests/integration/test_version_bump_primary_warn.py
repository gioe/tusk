"""Integration tests for the primary-on-default advisory in
`tusk version-bump` and `tusk changelog-add` (issue #923).

The advisory fires when ALL three conditions hold:
    a) CWD is NOT a recorded task_workspaces row (i.e. primary checkout).
    b) HEAD is on the repo's default branch.
    c) At least one task_workspaces row exists whose owning task is
       In Progress AND whose workspace_path exists on disk.

When any condition fails, the advisory is silent. The bump itself
always proceeds regardless of the advisory.

Cases covered (criteria 2309, 2310, 2311, 2312, 2313, 2314, 2315):
- Advisory fires for version-bump from primary on default branch with
  no --task-id; VERSION still bumped (criterion 1).
- Advisory fires for changelog-add under the same conditions
  (criterion 2).
- Advisory suppressed when --task-id is passed (criterion 3).
- Advisory suppressed when primary is on a non-default branch
  (criterion 4).
- Advisory suppressed when no in-progress task_workspaces rows exist
  (criterion 5).
- Advisory suppressed when invoked from inside the worktree (criterion 6).
"""

import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
ADVISORY_FRAGMENT = "invoked from primary on default branch"


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


def _tusk(args, *, cwd, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _env_without_quiet():
    env = os.environ.copy()
    env["TUSK_DB"] = os.environ["TUSK_DB"]
    env.pop("TUSK_QUIET", None)
    return env


def _seed_primary(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "VERSION").write_text("100\n", encoding="utf-8")
    (primary / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- placeholder\n", encoding="utf-8"
    )
    _git(["add", "VERSION", "CHANGELOG.md"], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)
    # Seed remote/origin/HEAD so default-branch resolution finds 'main' even
    # though no remote is configured. symbolic-ref against a missing ref
    # falls back to "main" inside _resolve_default_branch, but stamping it
    # explicitly mirrors what cloned repos look like.
    return primary


def _insert_task_with_workspace(db_path, workspace_path, *, status="In Progress", branch="feature/TASK-9001-test"):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score) "
        "VALUES ('test', ?, 'bug', 'Medium', 'S', 50)",
        (status,),
    )
    task_id = cur.lastrowid
    conn.execute(
        "INSERT INTO task_workspaces (task_id, branch, workspace_path) VALUES (?, ?, ?)",
        (task_id, branch, str(workspace_path)),
    )
    conn.commit()
    conn.close()
    return task_id


@pytest.fixture()
def primary_with_active_worktree(tmp_path, db_path):
    """Primary repo on `main` + a linked worktree on a feature branch +
    an In Progress task row whose task_workspaces row points at the
    worktree. The shape matches the production layout the advisory was
    designed for.
    """
    primary = _seed_primary(tmp_path)
    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/TASK-9001-test"], cwd=primary)
    task_id = _insert_task_with_workspace(db_path, worktree)
    return primary, worktree, task_id


def test_version_bump_advisory_fires_from_primary_on_default(primary_with_active_worktree):
    """Criterion 1: advisory text + bump still proceeds (exit 0)."""
    primary, worktree, task_id = primary_with_active_worktree
    env = _env_without_quiet()

    result = _tusk(["version-bump"], cwd=primary, env=env)

    assert result.returncode == 0, (
        f"version-bump must succeed; got {result.returncode}\nSTDERR: {result.stderr}"
    )
    assert result.stdout.strip() == "101"
    assert ADVISORY_FRAGMENT in result.stderr
    assert f"TASK-{task_id}" in result.stderr
    assert "--task-id" in result.stderr
    # The bump landed in primary (current behavior — advisory does not change routing).
    assert (primary / "VERSION").read_text(encoding="utf-8").strip() == "101"


def test_changelog_add_advisory_fires_from_primary_on_default(primary_with_active_worktree):
    """Criterion 2: same advisory for changelog-add."""
    primary, worktree, task_id = primary_with_active_worktree
    env = _env_without_quiet()

    result = _tusk(["changelog-add", "100"], cwd=primary, env=env)

    assert result.returncode == 0, (
        f"changelog-add must succeed; got {result.returncode}\nSTDERR: {result.stderr}"
    )
    assert ADVISORY_FRAGMENT in result.stderr
    assert f"TASK-{task_id}" in result.stderr
    # changelog-add still wrote primary's CHANGELOG.md.
    assert "## [100]" in (primary / "CHANGELOG.md").read_text(encoding="utf-8")


def test_advisory_suppressed_when_task_id_passed(primary_with_active_worktree):
    """Criterion 3: --task-id is explicit routing; no advisory."""
    primary, worktree, task_id = primary_with_active_worktree
    env = _env_without_quiet()

    result = _tusk(["version-bump", "--task-id", str(task_id)], cwd=primary, env=env)

    assert result.returncode == 0, result.stderr
    assert ADVISORY_FRAGMENT not in result.stderr
    # Bump landed in worktree, not primary.
    assert (worktree / "VERSION").read_text(encoding="utf-8").strip() == "101"
    assert (primary / "VERSION").read_text(encoding="utf-8").strip() == "100"


def test_advisory_suppressed_on_non_default_branch(tmp_path, db_path):
    """Criterion 4: primary on a feature branch is a deliberate operator
    action — no advisory.
    """
    primary = _seed_primary(tmp_path)
    _git(["checkout", "-b", "feature/local-work"], cwd=primary)
    # Still create an active worktree so condition (c) holds — only the
    # branch-name gate should suppress here.
    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/TASK-9001-test"], cwd=primary)
    _insert_task_with_workspace(db_path, worktree)
    env = _env_without_quiet()

    result = _tusk(["version-bump"], cwd=primary, env=env)

    assert result.returncode == 0, result.stderr
    assert ADVISORY_FRAGMENT not in result.stderr
    assert (primary / "VERSION").read_text(encoding="utf-8").strip() == "101"


def test_advisory_suppressed_when_no_active_workspace_rows(tmp_path, db_path):
    """Criterion 5: no in-progress task_workspaces rows → no candidate
    target → no advisory.
    """
    primary = _seed_primary(tmp_path)
    # Deliberately insert NO task_workspaces row.
    env = _env_without_quiet()

    result = _tusk(["version-bump"], cwd=primary, env=env)

    assert result.returncode == 0, result.stderr
    assert ADVISORY_FRAGMENT not in result.stderr
    assert (primary / "VERSION").read_text(encoding="utf-8").strip() == "101"


def test_advisory_suppressed_when_only_done_task_workspace_row(tmp_path, db_path):
    """Criterion 5 follow-on: a task_workspaces row whose task is Done is
    not a candidate — advisory still suppressed.
    """
    primary = _seed_primary(tmp_path)
    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/TASK-9002-done"], cwd=primary)
    _insert_task_with_workspace(db_path, worktree, status="Done", branch="feature/TASK-9002-done")
    env = _env_without_quiet()

    result = _tusk(["version-bump"], cwd=primary, env=env)

    assert result.returncode == 0, result.stderr
    assert ADVISORY_FRAGMENT not in result.stderr


def test_advisory_suppressed_when_invoked_from_worktree(primary_with_active_worktree):
    """Criterion 6: CWD inside the worktree → recorded workspace path
    match → no advisory.
    """
    primary, worktree, task_id = primary_with_active_worktree
    env = _env_without_quiet()

    result = _tusk(["version-bump"], cwd=worktree, env=env)

    assert result.returncode == 0, result.stderr
    assert ADVISORY_FRAGMENT not in result.stderr
    # Bump landed in the worktree (CWD-fallback behavior preserved).
    assert (worktree / "VERSION").read_text(encoding="utf-8").strip() == "101"


def test_tusk_quiet_silences_advisory(primary_with_active_worktree):
    """TUSK_QUIET=1 must silence the advisory even when all conditions
    fire. Mirrors the existing config-drift TTY/quiet convention.
    """
    primary, worktree, task_id = primary_with_active_worktree
    env = _env_without_quiet()
    env["TUSK_QUIET"] = "1"

    result = _tusk(["version-bump"], cwd=primary, env=env)

    assert result.returncode == 0, result.stderr
    assert ADVISORY_FRAGMENT not in result.stderr


def test_advisory_suppressed_when_workspace_path_missing_on_disk(tmp_path, db_path):
    """Criterion 5 edge case: a registered workspace whose directory was
    deleted is not a usable candidate — advisory must not name it.
    """
    primary = _seed_primary(tmp_path)
    missing = tmp_path / "ghost-worktree"
    _insert_task_with_workspace(db_path, missing, branch="feature/TASK-9003-ghost")
    env = _env_without_quiet()

    result = _tusk(["version-bump"], cwd=primary, env=env)

    assert result.returncode == 0, result.stderr
    assert ADVISORY_FRAGMENT not in result.stderr

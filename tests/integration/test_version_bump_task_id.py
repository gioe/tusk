"""Integration tests for `tusk version-bump --task-id <N>` (issue #903).

Before the fix, `cmd_version_bump` resolved VERSION against ``$REPO_ROOT/VERSION``
where ``REPO_ROOT`` is the CWD's ``.git`` walk-up. From the primary checkout
on the default branch — the most common CWD — that path silently landed in
the primary's VERSION, not the active task worktree's. The fix adds an
explicit ``--task-id N`` flag that resolves the matching workspace from the
``task_workspaces`` registry and writes/stages against that path.

Cases covered:
- Primary-CWD bump with ``--task-id`` writes the worktree's VERSION and
  stages it in the worktree's index; primary's VERSION is untouched.
- ``--task-id`` for a task with no recorded workspace exits non-zero with a
  clear error.
- ``--task-id`` for a task whose registered workspace was deleted on disk
  exits non-zero with a clear error.
- ``--task-id`` for a non-existent task exits non-zero with a clear error.
"""

import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


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


@pytest.fixture()
def primary_and_worktree(tmp_path, db_path, monkeypatch):
    """Seed a primary repo on `main` with a VERSION file and a linked
    worktree on a feature branch. Insert a task + task_workspaces row
    pointing at the worktree's path.

    Returns ``(primary, worktree, task_id)``.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "VERSION").write_text("100\n", encoding="utf-8")
    _git(["add", "VERSION"], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)

    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/TASK-1-bump"], cwd=primary)

    # Insert a tasks row + matching task_workspaces row pointing at the worktree.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score) "
        "VALUES ('test', 'In Progress', 'bug', 'Medium', 'S', 50)"
    )
    task_id = cur.lastrowid
    conn.execute(
        "INSERT INTO task_workspaces (task_id, branch, workspace_path) VALUES (?, ?, ?)",
        (task_id, "feature/TASK-1-bump", str(worktree)),
    )
    conn.commit()
    conn.close()

    return primary, worktree, task_id


def test_task_id_bumps_worktree_not_primary(primary_and_worktree, monkeypatch):
    """The issue #903 regression: from primary CWD on `main` with an active
    task worktree, `tusk version-bump --task-id N` lands the bump in the
    worktree's VERSION, NOT the primary's."""
    primary, worktree, task_id = primary_and_worktree
    env = os.environ.copy()
    env["TUSK_DB"] = os.environ["TUSK_DB"]
    env["TUSK_QUIET"] = "1"

    result = _tusk(["version-bump", "--task-id", str(task_id)], cwd=primary, env=env)

    assert result.returncode == 0, (
        f"expected exit 0; got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert result.stdout.strip() == "101"
    # Worktree's VERSION should have been bumped.
    assert (worktree / "VERSION").read_text(encoding="utf-8").strip() == "101"
    # Primary's VERSION must NOT have moved.
    assert (primary / "VERSION").read_text(encoding="utf-8").strip() == "100"
    # Worktree's index should show the staged VERSION.
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=worktree,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "VERSION" in staged.stdout.splitlines()
    # Primary's index must be empty.
    primary_staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=primary,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert primary_staged.stdout.strip() == "", (
        f"primary checkout's index should be empty; got: {primary_staged.stdout!r}"
    )


def test_task_id_refuses_when_workspace_missing(tmp_path, db_path, monkeypatch):
    """A task with no `task_workspaces` row should exit non-zero with a
    clear error rather than silently writing somewhere unexpected."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "VERSION").write_text("100\n", encoding="utf-8")
    _git(["add", "VERSION"], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score) "
        "VALUES ('test', 'In Progress', 'bug', 'Medium', 'S', 50)"
    )
    orphan_task_id = cur.lastrowid
    conn.commit()
    conn.close()

    env = os.environ.copy()
    env["TUSK_DB"] = os.environ["TUSK_DB"]
    env["TUSK_QUIET"] = "1"

    result = _tusk(
        ["version-bump", "--task-id", str(orphan_task_id)], cwd=primary, env=env
    )
    assert result.returncode != 0
    assert "no recorded task workspace" in result.stderr
    # Primary's VERSION must not have moved.
    assert (primary / "VERSION").read_text(encoding="utf-8").strip() == "100"


def test_task_id_refuses_when_workspace_path_missing(
    tmp_path, db_path, monkeypatch
):
    """A task whose registered workspace_path no longer exists on disk
    should exit non-zero rather than silently retargeting."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "VERSION").write_text("100\n", encoding="utf-8")
    _git(["add", "VERSION"], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score) "
        "VALUES ('test', 'In Progress', 'bug', 'Medium', 'S', 50)"
    )
    task_id = cur.lastrowid
    conn.execute(
        "INSERT INTO task_workspaces (task_id, branch, workspace_path) VALUES (?, ?, ?)",
        (task_id, "feature/TASK-2-missing", str(tmp_path / "does-not-exist")),
    )
    conn.commit()
    conn.close()

    env = os.environ.copy()
    env["TUSK_DB"] = os.environ["TUSK_DB"]
    env["TUSK_QUIET"] = "1"

    result = _tusk(["version-bump", "--task-id", str(task_id)], cwd=primary, env=env)
    assert result.returncode != 0
    assert "no longer exists on disk" in result.stderr
    assert (primary / "VERSION").read_text(encoding="utf-8").strip() == "100"


def test_task_id_unknown_task_exits_nonzero(tmp_path, db_path, monkeypatch):
    """A --task-id pointing at a non-existent row exits non-zero with the
    same 'no recorded task workspace' error path (the lookup query returns
    None for unknown IDs)."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "VERSION").write_text("100\n", encoding="utf-8")
    _git(["add", "VERSION"], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)

    env = os.environ.copy()
    env["TUSK_DB"] = os.environ["TUSK_DB"]
    env["TUSK_QUIET"] = "1"

    result = _tusk(["version-bump", "--task-id", "999999"], cwd=primary, env=env)
    assert result.returncode != 0
    assert "no recorded task workspace" in result.stderr


def test_changelog_add_task_id_writes_to_worktree(
    primary_and_worktree, monkeypatch
):
    """Direct exercise of `tusk changelog-add --task-id <N>` from primary CWD.

    The fix shares the same `resolve_task_workspace` helper across both
    commands, so confirming changelog-add lands at the workspace's
    CHANGELOG.md proves criterion 2 separately from version-bump.
    """
    primary, worktree, task_id = primary_and_worktree

    # Seed CHANGELOG.md in each checkout independently. The worktree branch
    # was already cut from main before this point, so a commit in primary
    # would not propagate. Writing the file directly into each checkout
    # is enough — changelog-add only needs the file to exist.
    template = "# Changelog\n\n## [Unreleased]\n\n- placeholder\n"
    (primary / "CHANGELOG.md").write_text(template, encoding="utf-8")
    (worktree / "CHANGELOG.md").write_text(template, encoding="utf-8")

    env = os.environ.copy()
    env["TUSK_DB"] = os.environ["TUSK_DB"]
    env["TUSK_QUIET"] = "1"

    result = _tusk(
        [
            "changelog-add",
            "--task-id",
            str(task_id),
            "100",
            str(task_id),
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 0, (
        f"expected exit 0; got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    worktree_changelog = (worktree / "CHANGELOG.md").read_text(encoding="utf-8")
    primary_changelog = (primary / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [100]" in worktree_changelog
    assert "## [100]" not in primary_changelog, (
        "primary CHANGELOG.md must NOT have been modified"
    )


def test_changelog_add_task_id_refuses_missing_workspace(
    tmp_path, db_path, monkeypatch
):
    """--task-id on an orphan task exits non-zero and does not write the
    primary's CHANGELOG.md."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "VERSION").write_text("100\n", encoding="utf-8")
    (primary / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n", encoding="utf-8"
    )
    _git(["add", "VERSION", "CHANGELOG.md"], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score) "
        "VALUES ('test', 'In Progress', 'bug', 'Medium', 'S', 50)"
    )
    orphan = cur.lastrowid
    conn.commit()
    conn.close()

    env = os.environ.copy()
    env["TUSK_DB"] = os.environ["TUSK_DB"]
    env["TUSK_QUIET"] = "1"

    before = (primary / "CHANGELOG.md").read_text(encoding="utf-8")
    result = _tusk(
        ["changelog-add", "--task-id", str(orphan), "100"], cwd=primary, env=env
    )
    assert result.returncode != 0
    assert "no recorded task workspace" in result.stderr
    after = (primary / "CHANGELOG.md").read_text(encoding="utf-8")
    assert before == after, (
        "primary CHANGELOG.md must not be modified when --task-id refusal fires"
    )


def test_no_task_id_keeps_cwd_fallback(tmp_path, db_path, monkeypatch):
    """Regression guard for criterion 4: omitting --task-id preserves the
    CWD-based fallback (the worktree-aware behavior from issues #798/#801).
    Bumping from a worktree CWD still writes the worktree's VERSION."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "VERSION").write_text("500\n", encoding="utf-8")
    _git(["add", "VERSION"], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)

    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/legacy"], cwd=primary)

    env = os.environ.copy()
    env["TUSK_DB"] = os.environ["TUSK_DB"]
    env["TUSK_QUIET"] = "1"

    result = _tusk(["version-bump"], cwd=worktree, env=env)
    assert result.returncode == 0, result.stderr
    assert (worktree / "VERSION").read_text(encoding="utf-8").strip() == "501"
    assert (primary / "VERSION").read_text(encoding="utf-8").strip() == "500"

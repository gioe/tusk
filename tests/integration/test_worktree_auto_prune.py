"""Integration tests for the auto-prune-on-create hook (TASK-477).

`tusk task-worktree create` runs the same staleness filter as
`tusk task-worktree prune` at the top of every call, scoped to OTHER tasks'
rows so the per-task reconcile logic (issue #803) is preserved for the
current task. Disabled via TUSK_NO_AUTO_PRUNE=1.
"""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(args, *, cwd, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


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


def _repo_with_tusk(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)

    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    monkeypatch.setenv("TUSK_DB", str(db_path))
    monkeypatch.setenv("TUSK_QUIET", "1")

    result = _run(["init", "--force", "--skip-gitignore"], cwd=repo, env=env)
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return repo, db_path, env


def _insert_task(db_path):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score) "
            "VALUES ('auto-prune task', 'create a worktree', 'To Do', 'feature', "
            "'High', 'M', 30)"
        )
        conn.commit()
        return cur.lastrowid


def _make_stale(repo, env, task_id, slug, workspace_root):
    """Create a registry row for ``task_id`` then forcibly tear down its worktree.

    After this returns, the row exists in ``task_workspaces`` but its
    ``workspace_path`` is gone from disk AND from ``git worktree list`` —
    i.e. ``_is_stale_workspace`` returns True. The branch ref is also
    removed so the create caller's issue #803 reconcile path doesn't try
    to re-attach if this same task is named again.
    """
    created = _run(
        [
            "task-worktree",
            "create",
            str(task_id),
            slug,
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    _git(["worktree", "remove", "--force", payload["workspace_path"]], cwd=repo)
    _git(["branch", "-D", payload["branch"]], cwd=repo)
    return payload


def test_runs_before_create(tmp_path, monkeypatch):
    """A sibling task's stale registry row is auto-pruned at create time."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    stale_task = _insert_task(db_path)
    fresh_task = _insert_task(db_path)
    workspace_root = tmp_path / "workspaces"

    stale_payload = _make_stale(repo, env, stale_task, "stale", workspace_root)

    fresh = _run(
        [
            "task-worktree",
            "create",
            str(fresh_task),
            "fresh",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert fresh.returncode == 0, fresh.stderr
    fresh_payload = json.loads(fresh.stdout)
    assert fresh_payload["created"] is True

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT task_id FROM task_workspaces ORDER BY task_id"
        ).fetchall()
    task_ids = [r[0] for r in rows]
    assert stale_task not in task_ids, (
        f"expected sibling stale row for TASK-{stale_task} to be auto-pruned; "
        f"registry still contains {task_ids}"
    )
    assert fresh_task in task_ids
    # Sanity: the fresh worktree itself is healthy.
    assert os.path.isdir(fresh_payload["workspace_path"])
    # Stale workspace_path is gone — auto-prune did not resurrect it.
    assert not os.path.isdir(stale_payload["workspace_path"])


def test_env_var_disables(tmp_path, monkeypatch):
    """TUSK_NO_AUTO_PRUNE=1 disables the auto-prune; stale row survives."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    stale_task = _insert_task(db_path)
    fresh_task = _insert_task(db_path)
    workspace_root = tmp_path / "workspaces"

    _make_stale(repo, env, stale_task, "stale-disabled", workspace_root)

    env_disabled = dict(env)
    env_disabled["TUSK_NO_AUTO_PRUNE"] = "1"

    fresh = _run(
        [
            "task-worktree",
            "create",
            str(fresh_task),
            "fresh-disabled",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env_disabled,
    )
    assert fresh.returncode == 0, fresh.stderr

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT task_id FROM task_workspaces ORDER BY task_id"
        ).fetchall()
    task_ids = sorted(r[0] for r in rows)
    assert task_ids == sorted([stale_task, fresh_task]), (
        f"TUSK_NO_AUTO_PRUNE=1 should have left the stale row in place; "
        f"got task_ids={task_ids}"
    )


def test_rules_match_manual(tmp_path, monkeypatch):
    """Auto-prune only removes rows that manual prune would also remove.

    Setup mixes one fully-stale row (path gone, no git entry, branch gone)
    with one live row (workspace dir + git entry present). After a fresh
    create, only the stale row is removed — the live one survives untouched,
    matching `tusk task-worktree prune`'s behavior.
    """
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    stale_task = _insert_task(db_path)
    live_task = _insert_task(db_path)
    new_task = _insert_task(db_path)
    workspace_root = tmp_path / "workspaces"

    _make_stale(repo, env, stale_task, "stale-mixed", workspace_root)

    live_created = _run(
        [
            "task-worktree",
            "create",
            str(live_task),
            "live",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert live_created.returncode == 0, live_created.stderr
    live_payload = json.loads(live_created.stdout)
    assert os.path.isdir(live_payload["workspace_path"])

    new_created = _run(
        [
            "task-worktree",
            "create",
            str(new_task),
            "new",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert new_created.returncode == 0, new_created.stderr

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT task_id FROM task_workspaces ORDER BY task_id"
        ).fetchall()
    task_ids = sorted(r[0] for r in rows)
    assert task_ids == sorted([live_task, new_task]), (
        f"auto-prune should only have removed the fully-stale sibling row; "
        f"got task_ids={task_ids}"
    )
    # Live worktree dir untouched.
    assert os.path.isdir(live_payload["workspace_path"])


def test_clean_create(tmp_path, monkeypatch):
    """A clean create with nothing to prune still succeeds normally."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task_id = _insert_task(db_path)
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task_id),
            "clean",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["created"] is True
    assert payload["task_id"] == task_id
    assert os.path.isdir(payload["workspace_path"])

    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM task_workspaces").fetchone()[0]
    assert count == 1

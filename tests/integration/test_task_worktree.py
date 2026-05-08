"""Integration tests for normal task-owned worktree commands."""

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
            "VALUES ('worktree task', 'create a worktree', 'To Do', 'feature', "
            "'High', 'M', 30)"
        )
        conn.commit()
        return cur.lastrowid


class TestTaskWorktreeList:
    def test_empty_list_defaults_to_json_array(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)

        result = _run(["task-worktree", "list"], cwd=repo, env=env)

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == []
        with sqlite3.connect(db_path) as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'task_workspaces'"
            ).fetchone()
        assert table is not None

    def test_list_reports_missing_worktree_on_disk(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"
        created = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "missing-disk",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert created.returncode == 0, created.stderr
        payload = json.loads(created.stdout)
        _git(["worktree", "remove", "--force", payload["workspace_path"]], cwd=repo)

        result = _run(["task-worktree", "list"], cwd=repo, env=env)

        assert result.returncode == 0, result.stderr
        rows = json.loads(result.stdout)
        assert len(rows) == 1
        assert rows[0]["workspace_id"] == payload["workspace_id"]
        assert rows[0]["exists_on_disk"] is False
        assert rows[0]["live_workspace_path"] is None


class TestTaskWorktreeCreate:
    def test_create_and_reuse_worktree_for_task(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        first = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "task-workspace",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert first.returncode == 0, first.stderr
        first_payload = json.loads(first.stdout)
        assert first_payload["task_id"] == task_id
        assert first_payload["branch"] == f"feature/TASK-{task_id}-task-workspace"
        assert first_payload["workspace_id"] > 0
        assert first_payload["created"] is True
        assert os.path.isdir(first_payload["workspace_path"])

        second = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "task-workspace",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert second.returncode == 0, second.stderr
        second_payload = json.loads(second.stdout)
        assert second_payload["created"] is False
        assert second_payload["workspace_id"] == first_payload["workspace_id"]
        assert second_payload["workspace_path"] == first_payload["workspace_path"]

    def test_create_rejects_branch_collision_without_record(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        branch = f"feature/TASK-{task_id}-collision"
        _git(["branch", branch], cwd=repo)

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "collision",
                "--workspace-root",
                str(tmp_path / "workspaces"),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 2
        assert "already exists" in result.stderr

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


def _git_with_env(args, *, cwd, env):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
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


def _insert_session(db_path, task_id):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO task_sessions (task_id, started_at) "
            "VALUES (?, datetime('now'))",
            (task_id,),
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

    def test_create_bases_worktree_on_fetched_origin_default(
        self, tmp_path, monkeypatch
    ):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        origin = tmp_path / "origin.git"
        _git(["init", "--bare", str(origin)], cwd=tmp_path)
        _git(["remote", "add", "origin", str(origin)], cwd=repo)
        _git(["push", "-u", "origin", "main"], cwd=repo)
        _git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=origin)

        remote_clone = tmp_path / "remote-clone"
        _git(["clone", str(origin), str(remote_clone)], cwd=tmp_path)
        _git(["config", "user.email", "tusk@example.test"], cwd=remote_clone)
        _git(["config", "user.name", "Tusk Tests"], cwd=remote_clone)
        remote_only_file = remote_clone / "remote-only.txt"
        remote_only_file.write_text("remote default branch work\n", encoding="utf-8")
        _git(["add", "remote-only.txt"], cwd=remote_clone)
        _git(["commit", "-m", "advance remote default"], cwd=remote_clone)
        _git(["push", "origin", "main"], cwd=remote_clone)

        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "fresh-origin",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert os.path.exists(os.path.join(payload["workspace_path"], "remote-only.txt"))


class TestTaskWorktreeCloseout:
    def test_merge_prefers_recorded_task_workspace_branch(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        session_id = _insert_session(db_path, task_id)
        workspace_root = tmp_path / "workspaces"

        created = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "recorded",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert created.returncode == 0, created.stderr
        payload = json.loads(created.stdout)

        recorded_file = os.path.join(payload["workspace_path"], "recorded.txt")
        with open(recorded_file, "w", encoding="utf-8") as handle:
            handle.write("recorded work\n")
        _git(["add", "recorded.txt"], cwd=payload["workspace_path"])
        _git(
            ["commit", "-m", f"[TASK-{task_id}] recorded branch work"],
            cwd=payload["workspace_path"],
        )

        other_workspace = workspace_root / "other"
        other_branch = f"feature/TASK-{task_id}-unrecorded"
        _git(["worktree", "add", "-b", other_branch, str(other_workspace), "main"], cwd=repo)
        other_file = other_workspace / "unrecorded.txt"
        other_file.write_text("wrong branch\n", encoding="utf-8")
        _git(["add", "unrecorded.txt"], cwd=other_workspace)
        later_env = os.environ.copy()
        later_env.update(
            {
                "GIT_AUTHOR_DATE": "2030-01-01T00:00:00+0000",
                "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+0000",
            }
        )
        _git_with_env(
            ["commit", "-m", f"[TASK-{task_id}] unrecorded branch work"],
            cwd=other_workspace,
            env=later_env,
        )

        result = _run(
            ["merge", str(task_id), "--session", str(session_id)],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert os.path.exists(repo / "recorded.txt")
        assert not os.path.exists(repo / "unrecorded.txt")
        assert os.path.isdir(other_workspace)
        assert other_branch in _git(["branch", "--list", other_branch], cwd=repo).stdout

    def test_merge_removes_only_completed_task_worktree(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        first_task = _insert_task(db_path)
        first_session = _insert_session(db_path, first_task)
        second_task = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        first_created = _run(
            [
                "task-worktree",
                "create",
                str(first_task),
                "merge-me",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert first_created.returncode == 0, first_created.stderr
        first_payload = json.loads(first_created.stdout)
        second_created = _run(
            [
                "task-worktree",
                "create",
                str(second_task),
                "keep-me",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert second_created.returncode == 0, second_created.stderr
        second_payload = json.loads(second_created.stdout)

        first_workspace = first_payload["workspace_path"]
        feature_file = os.path.join(first_workspace, "feature.txt")
        with open(feature_file, "w", encoding="utf-8") as handle:
            handle.write("feature work\n")
        _git(["add", "feature.txt"], cwd=first_workspace)
        _git(["commit", "-m", f"[TASK-{first_task}] add feature work"], cwd=first_workspace)

        result = _run(
            ["merge", str(first_task), "--session", str(first_session)],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert not os.path.exists(first_payload["workspace_path"])
        assert os.path.isdir(second_payload["workspace_path"])
        assert (
            _git(["branch", "--list", first_payload["branch"]], cwd=repo).stdout.strip()
            == ""
        )
        assert os.path.exists(repo / "feature.txt")

    def test_abandon_dirty_task_worktree_refuses_cleanup_before_closing(
        self, tmp_path, monkeypatch
    ):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        session_id = _insert_session(db_path, task_id)
        created = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "dirty-abandon",
                "--workspace-root",
                str(tmp_path / "workspaces"),
            ],
            cwd=repo,
            env=env,
        )
        assert created.returncode == 0, created.stderr
        payload = json.loads(created.stdout)

        dirty_file = os.path.join(payload["workspace_path"], "scratch.txt")
        with open(dirty_file, "w", encoding="utf-8") as handle:
            handle.write("uncommitted\n")

        result = _run(
            [
                "abandon",
                str(task_id),
                "--reason",
                "wont_do",
                "--session",
                str(session_id),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 2
        assert "dirty" in result.stderr.lower()
        assert "git worktree remove" in result.stderr
        assert os.path.isdir(payload["workspace_path"])
        with sqlite3.connect(db_path) as conn:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            session = conn.execute(
                "SELECT ended_at FROM task_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        assert task[0] == "To Do"
        assert session[0] is None

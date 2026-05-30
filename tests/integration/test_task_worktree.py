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


class TestTaskWorktreePrune:
    def test_prune_removes_stale_missing_worktree_rows(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"
        created = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "stale-row",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert created.returncode == 0, created.stderr
        payload = json.loads(created.stdout)
        _git(["worktree", "remove", "--force", payload["workspace_path"]], cwd=repo)

        result = _run(["task-worktree", "prune"], cwd=repo, env=env)

        assert result.returncode == 0, result.stderr
        prune_payload = json.loads(result.stdout)
        assert prune_payload["dry_run"] is False
        assert prune_payload["removed_count"] == 1
        assert [row["workspace_id"] for row in prune_payload["removed"]] == [
            payload["workspace_id"]
        ]
        with sqlite3.connect(db_path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM task_workspaces").fetchone()[0]
        assert remaining == 0

    def test_prune_dry_run_reports_stale_rows_without_deleting(
        self, tmp_path, monkeypatch
    ):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"
        created = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "dry-run",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert created.returncode == 0, created.stderr
        payload = json.loads(created.stdout)
        _git(["worktree", "remove", "--force", payload["workspace_path"]], cwd=repo)

        result = _run(["task-worktree", "prune", "--dry-run"], cwd=repo, env=env)

        assert result.returncode == 0, result.stderr
        prune_payload = json.loads(result.stdout)
        assert prune_payload["dry_run"] is True
        assert prune_payload["removed_count"] == 1
        assert [row["workspace_id"] for row in prune_payload["removed"]] == [
            payload["workspace_id"]
        ]
        with sqlite3.connect(db_path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM task_workspaces").fetchone()[0]
        assert remaining == 1

    def test_prune_preserves_live_recorded_worktrees(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        stale_task = _insert_task(db_path)
        live_task = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"
        stale_created = _run(
            [
                "task-worktree",
                "create",
                str(stale_task),
                "stale",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert stale_created.returncode == 0, stale_created.stderr
        stale_payload = json.loads(stale_created.stdout)
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
        _git(["worktree", "remove", "--force", stale_payload["workspace_path"]], cwd=repo)

        result = _run(["task-worktree", "prune"], cwd=repo, env=env)

        assert result.returncode == 0, result.stderr
        prune_payload = json.loads(result.stdout)
        assert [row["workspace_id"] for row in prune_payload["removed"]] == [
            stale_payload["workspace_id"]
        ]
        with sqlite3.connect(db_path) as conn:
            remaining = conn.execute(
                "SELECT id FROM task_workspaces ORDER BY id"
            ).fetchall()
        assert [row[0] for row in remaining] == [live_payload["workspace_id"]]
        assert os.path.isdir(live_payload["workspace_path"])


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


class TestTaskWorktreeCreateIdempotentOnTaskId:
    """Issue #947 — task-worktree create must be idempotent on task_id. When a
    task already has a recorded workspace, a second create under a DIFFERENT
    slug must reuse the existing workspace (created:false), not provision a
    second worktree + branch. The lookup keyed on (task_id, branch) silently
    duplicated whenever the resuming agent picked a new slug.
    """

    def test_different_slug_reuses_existing_workspace(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        first = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "slug-a",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert first.returncode == 0, first.stderr
        first_payload = json.loads(first.stdout)
        assert first_payload["created"] is True

        # Second create under a DIFFERENT slug — must NOT create a second one.
        second = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "slug-b-different",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert second.returncode == 0, second.stderr
        second_payload = json.loads(second.stdout)
        # Reused the existing workspace rather than provisioning a new one.
        assert second_payload["created"] is False
        assert second_payload["workspace_id"] == first_payload["workspace_id"]
        assert second_payload["workspace_path"] == first_payload["workspace_path"]
        assert second_payload["branch"] == first_payload["branch"]
        # The requested-but-ignored slug is surfaced on stderr.
        assert "slug-b-different" in second.stderr

        # Exactly one registry row for the task — no duplicate workspace.
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM task_workspaces WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        assert [row[0] for row in rows] == [first_payload["workspace_id"]]

        # Exactly one task row in the live list view too.
        listed = _run(["task-worktree", "list"], cwd=repo, env=env)
        assert listed.returncode == 0, listed.stderr
        task_rows = [
            r for r in json.loads(listed.stdout) if r["task_id"] == task_id
        ]
        assert len(task_rows) == 1


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

    def test_abandon_dirty_task_worktree_refuses_cleanup_after_closing(
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
        assert task[0] == "Done"
        assert session[0] is not None


class TestTaskWorktreeCreateSymlinks:
    """Issue #752 — worktree.symlink_files config opts the project into
    seeding new task worktrees with absolute symlinks for gitignored runtime
    files (e.g. .venv, .env) from the primary repo. Default empty list keeps
    existing behavior bit-for-bit.
    """

    def _set_symlink_files(self, repo, names):
        """Patch worktree.symlink_files in the repo's tusk/config.json."""
        cfg_path = os.path.join(str(repo), "tusk", "config.json")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        worktree_cfg = cfg.setdefault("worktree", {})
        worktree_cfg["symlink_files"] = list(names)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def _plant_files(self, repo, paths_and_content):
        """Create files (and parent dirs) in the primary repo with content."""
        for rel, content in paths_and_content:
            full = os.path.join(str(repo), rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)

    def test_default_empty_config_creates_no_symlinks_when_opted_out(
        self, tmp_path, monkeypatch
    ):
        """With worktree.symlink_files=[] AND TUSK_NO_AUTO_SYMLINK=1, no
        symlinks are created even when canonical runtime artifacts are
        present in the primary checkout (issue #854 opt-out path).
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        env = dict(env)
        env["TUSK_NO_AUTO_SYMLINK"] = "1"
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"
        self._plant_files(repo, [(".venv/marker", "venv-content")])
        self._plant_files(repo, [(".env", "ENV=1")])

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "no-symlinks",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        assert not os.path.lexists(os.path.join(wt, ".venv"))
        assert not os.path.lexists(os.path.join(wt, ".env"))

    def test_top_level_symlinks_created_for_configured_basenames(
        self, tmp_path, monkeypatch
    ):
        """When worktree.symlink_files is set, matching primary entries are
        exposed in the worktree as absolute symlinks.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._set_symlink_files(repo, [".venv", ".env"])
        self._plant_files(
            repo,
            [(".venv/bin/python", "fake-py"), (".env", "DB_URL=local")],
        )
        primary_venv = os.path.join(str(repo), ".venv")
        primary_env = os.path.join(str(repo), ".env")
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "with-symlinks",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        wt_venv = os.path.join(wt, ".venv")
        wt_env = os.path.join(wt, ".env")
        assert os.path.islink(wt_venv), "expected .venv to be a symlink"
        assert os.path.islink(wt_env), "expected .env to be a symlink"
        # Symlink targets must be absolute paths so they survive worktree relocation.
        assert os.readlink(wt_venv) == primary_venv
        assert os.readlink(wt_env) == primary_env
        # And the symlinks must resolve to readable content from the primary.
        assert os.path.exists(os.path.join(wt_venv, "bin", "python"))

    def test_nested_symlinks_match_at_corresponding_subdir(
        self, tmp_path, monkeypatch
    ):
        """The walk is recursive: a .venv under apps/scraper in the primary
        should land at apps/scraper/.venv in the worktree.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._set_symlink_files(repo, [".venv"])
        self._plant_files(
            repo, [("apps/scraper/.venv/marker", "scraper-venv")]
        )
        primary_nested = os.path.join(str(repo), "apps", "scraper", ".venv")
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "nested",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        wt_nested = os.path.join(wt, "apps", "scraper", ".venv")
        assert os.path.islink(wt_nested), (
            f"expected nested .venv symlink at {wt_nested}"
        )
        assert os.readlink(wt_nested) == primary_nested

    def test_missing_primary_file_is_skipped_silently(self, tmp_path, monkeypatch):
        """If the basename is configured but no matching file exists in the
        primary, no symlink is created (and no error is raised).
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._set_symlink_files(repo, [".venv", ".missing"])
        self._plant_files(repo, [(".venv/marker", "venv")])
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "partial",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        assert os.path.islink(os.path.join(wt, ".venv"))
        assert not os.path.lexists(os.path.join(wt, ".missing"))

    def test_git_directory_is_excluded_from_walk(self, tmp_path, monkeypatch):
        """The walk must skip .git so we don't symlink anything from the git
        metadata directory, even when a basename collision exists.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        # `config` exists at .git/config in every git repo. The walk must NOT pick it up.
        self._set_symlink_files(repo, ["config"])
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "skip-git",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        assert not os.path.lexists(os.path.join(wt, ".git", "config"))


class TestTaskWorktreeCreateCanonicalFallback:
    """Issue #854 — when worktree.symlink_files is empty (install.sh installs
    never invoke the init-write-config auto-seed), task-worktree create falls
    back to a canonical name set (node_modules, .venv, .env, .env.local) and
    emits a stderr advisory pointing at /tusk-update. Explicit config always
    wins; TUSK_NO_AUTO_SYMLINK=1 disables the fallback.
    """

    def _set_symlink_files(self, repo, names):
        cfg_path = os.path.join(str(repo), "tusk", "config.json")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("worktree", {})["symlink_files"] = list(names)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def _plant_files(self, repo, paths_and_content):
        for rel, content in paths_and_content:
            full = os.path.join(str(repo), rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)

    def test_fallback_links_nested_node_modules_with_empty_config(
        self, tmp_path, monkeypatch
    ):
        """Empty worktree.symlink_files + apps/web/node_modules in primary →
        nested node_modules is symlinked AND advisory is printed.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._plant_files(repo, [("apps/web/node_modules/vitest/bin", "fake")])
        primary_nm = os.path.join(str(repo), "apps", "web", "node_modules")
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "auto-link",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        wt_nm = os.path.join(wt, "apps", "web", "node_modules")
        assert os.path.islink(wt_nm), f"expected node_modules symlink at {wt_nm}"
        assert os.readlink(wt_nm) == primary_nm
        # Advisory criterion: named basenames + /tusk-update pointer + opt-out
        assert "auto-linked" in result.stderr
        assert "node_modules" in result.stderr
        assert "/tusk-update" in result.stderr
        assert "TUSK_NO_AUTO_SYMLINK" in result.stderr

    def test_explicit_config_wins_over_canonical_fallback(
        self, tmp_path, monkeypatch
    ):
        """Non-empty worktree.symlink_files suppresses the fallback even when
        canonical artifacts are present. Linked set must equal the explicit
        list — no canonical names leak in.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        # Explicit list contains ONLY one canonical name (.venv); node_modules
        # exists in primary but must NOT be linked.
        self._set_symlink_files(repo, [".venv"])
        self._plant_files(repo, [(".venv/marker", "v")])
        self._plant_files(repo, [("apps/web/node_modules/p/x", "n")])
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "explicit-wins",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        assert os.path.islink(os.path.join(wt, ".venv"))
        assert not os.path.lexists(os.path.join(wt, "apps", "web", "node_modules"))
        # No fallback advisory when config was explicit (even though links happened).
        assert "auto-linked" not in result.stderr

    def test_opt_out_env_disables_fallback_even_with_artifacts(
        self, tmp_path, monkeypatch
    ):
        """TUSK_NO_AUTO_SYMLINK=1 disables the fallback completely; no
        symlinks created and no advisory emitted even when canonical
        artifacts are present and config is empty.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        env = dict(env)
        env["TUSK_NO_AUTO_SYMLINK"] = "1"
        self._plant_files(repo, [("apps/web/node_modules/p/x", "n")])
        self._plant_files(repo, [(".env", "K=V")])
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "opt-out",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        assert not os.path.lexists(os.path.join(wt, "apps", "web", "node_modules"))
        assert not os.path.lexists(os.path.join(wt, ".env"))
        assert "auto-linked" not in result.stderr

    def test_no_advisory_when_no_canonical_artifacts_present(
        self, tmp_path, monkeypatch
    ):
        """Empty config + no canonical artifacts in primary → walk runs but
        creates zero symlinks; advisory must NOT print (its trigger is
        ≥1 created link).
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        # Plant a non-canonical file so the walk runs but matches nothing.
        self._plant_files(repo, [("src/main.py", "print('hi')")])
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "no-advisory",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "auto-linked" not in result.stderr


class TestTaskWorktreeCreatePathStyleSymlinks:
    """Issue #867 — worktree.symlink_files entries containing ``/`` are
    treated as project-relative paths and link exactly once at that location,
    instead of being silently dropped by the bare-basename matcher. Lets
    monorepo users scope a symlink to one specific subdirectory (e.g.
    ``apps/web/node_modules``) without over-matching every nested copy.
    """

    def _set_symlink_files(self, repo, names):
        cfg_path = os.path.join(str(repo), "tusk", "config.json")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("worktree", {})["symlink_files"] = list(names)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def _plant_files(self, repo, paths_and_content):
        for rel, content in paths_and_content:
            full = os.path.join(str(repo), rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)

    def test_path_style_entry_links_exact_subdir_only(self, tmp_path, monkeypatch):
        """Configured 'apps/web/node_modules' creates exactly one symlink at
        that relative path; sibling nested node_modules are NOT linked.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._set_symlink_files(repo, ["apps/web/node_modules"])
        self._plant_files(
            repo,
            [
                ("apps/web/node_modules/vitest/bin", "fake-vitest"),
                ("apps/api/node_modules/tsx/bin", "fake-tsx"),
            ],
        )
        primary_target = os.path.join(
            str(repo), "apps", "web", "node_modules"
        )
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "path-style",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        wt_target = os.path.join(wt, "apps", "web", "node_modules")
        wt_sibling = os.path.join(wt, "apps", "api", "node_modules")
        assert os.path.islink(wt_target), (
            f"expected path-style symlink at {wt_target}"
        )
        assert os.readlink(wt_target) == primary_target
        # The sibling path-style miss must NOT be linked — that's the whole
        # point of path-style over bare basename in a monorepo.
        assert not os.path.lexists(wt_sibling)
        # Symlink content is readable from the primary.
        assert os.path.exists(os.path.join(wt_target, "vitest", "bin"))

    def test_path_style_entry_missing_primary_skipped_silently(
        self, tmp_path, monkeypatch
    ):
        """A path-style entry whose primary target does not exist is skipped
        — no symlink, no error — consistent with bare-basename miss behavior.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._set_symlink_files(repo, ["apps/web/node_modules"])
        # Plant a sibling file so the worktree exists but the target doesn't.
        self._plant_files(repo, [("apps/web/src/main.ts", "console.log(1)")])
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "path-style-miss",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        assert not os.path.lexists(os.path.join(wt, "apps", "web", "node_modules"))

    def test_mixed_basename_and_path_style_both_honored(
        self, tmp_path, monkeypatch
    ):
        """A config mixing bare basenames and path-style entries honors each
        per its kind: basename walks-and-matches, path-style links exactly
        the configured location.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._set_symlink_files(repo, [".env", "apps/web/node_modules"])
        self._plant_files(
            repo,
            [
                (".env", "TOP=1"),
                ("apps/web/.env", "WEB=1"),  # nested .env — basename should match
                ("apps/web/node_modules/p/x", "n"),
                ("apps/api/node_modules/p/x", "n"),  # NOT in path-style list
            ],
        )
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "mixed",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt = payload["workspace_path"]
        # Bare-basename .env: matched at every depth.
        assert os.path.islink(os.path.join(wt, ".env"))
        assert os.path.islink(os.path.join(wt, "apps", "web", ".env"))
        # Path-style apps/web/node_modules: matched at the exact path only.
        assert os.path.islink(
            os.path.join(wt, "apps", "web", "node_modules")
        )
        # apps/api/node_modules: NOT in the list, must not be linked.
        assert not os.path.lexists(
            os.path.join(wt, "apps", "api", "node_modules")
        )


class TestTaskWorktreeCreateNodeModulesFreshness:
    """Issue #960 — create should warn when a materialized node_modules is
    older than the adjacent package manifest or lockfile.
    """

    def _set_symlink_files(self, repo, names):
        cfg_path = os.path.join(str(repo), "tusk", "config.json")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("worktree", {})["symlink_files"] = list(names)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def _plant_files(self, repo, paths_and_content):
        for rel, content in paths_and_content:
            full = os.path.join(str(repo), rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)

    def _commit_package_files(self, repo, *paths):
        _git(["add", *paths], cwd=repo)
        _git(["commit", "-m", "add package manifests"], cwd=repo)

    def _age_dir(self, path, timestamp=1_700_000_000):
        os.utime(path, (timestamp, timestamp))

    def test_fallback_warns_when_nested_node_modules_is_older_than_package_json(
        self, tmp_path, monkeypatch
    ):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._plant_files(
            repo,
            [
                ("apps/web/package.json", '{"dependencies":{"left-pad":"1.3.0"}}'),
                ("apps/web/node_modules/left-pad/index.js", "module.exports = 1"),
            ],
        )
        self._commit_package_files(repo, "apps/web/package.json")
        self._age_dir(os.path.join(str(repo), "apps", "web", "node_modules"))
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "stale-fallback",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "apps/web/node_modules may be stale" in result.stderr
        assert "package.json" in result.stderr
        assert "apps/web" in result.stderr
        assert "package install command" in result.stderr

    def test_path_style_warning_names_only_the_configured_package_dir(
        self, tmp_path, monkeypatch
    ):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._set_symlink_files(repo, ["apps/web/node_modules"])
        self._plant_files(
            repo,
            [
                ("apps/web/package-lock.json", '{"lockfileVersion":3}'),
                ("apps/web/node_modules/pkg/index.js", "web"),
                ("apps/api/package-lock.json", '{"lockfileVersion":3}'),
                ("apps/api/node_modules/pkg/index.js", "api"),
            ],
        )
        self._commit_package_files(
            repo, "apps/web/package-lock.json", "apps/api/package-lock.json"
        )
        self._age_dir(os.path.join(str(repo), "apps", "web", "node_modules"))
        self._age_dir(os.path.join(str(repo), "apps", "api", "node_modules"))
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "stale-path-style",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "apps/web/node_modules may be stale" in result.stderr
        assert "package-lock.json" in result.stderr
        assert "apps/api/node_modules may be stale" not in result.stderr

    def test_no_warning_when_node_modules_is_newer_than_manifest(
        self, tmp_path, monkeypatch
    ):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        self._plant_files(
            repo,
            [
                ("apps/web/package.json", '{"dependencies":{"left-pad":"1.3.0"}}'),
                ("apps/web/node_modules/left-pad/index.js", "module.exports = 1"),
            ],
        )
        self._commit_package_files(repo, "apps/web/package.json")
        self._age_dir(os.path.join(str(repo), "apps", "web", "node_modules"), 4_100_000_000)
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "fresh-fallback",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "may be stale" not in result.stderr


class TestTaskWorktreeCreateStaleRow:
    """Issue #803 — when task_workspaces has a row but workspace_path is gone
    from disk, the old behavior returned `created:false` and the caller `cd`'d
    into a dangling path. cmd_create now reconciles: re-attach when the branch
    survives, refuse loudly when both row and branch are stale.
    """

    def _make_recorded_workspace(self, tmp_path, monkeypatch, slug="stale-row"):
        """Create a recorded workspace and return (repo, db_path, env, task_id, payload)."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"
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
        return repo, db_path, env, task_id, json.loads(created.stdout)

    def test_create_returns_unchanged_when_workspace_is_healthy(
        self, tmp_path, monkeypatch
    ):
        """Healthy path: row exists, workspace_path exists on disk → created:false."""
        repo, db_path, env, task_id, payload = self._make_recorded_workspace(
            tmp_path, monkeypatch
        )
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "stale-row",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["created"] is False
        assert body["workspace_path"] == payload["workspace_path"]
        assert os.path.isdir(body["workspace_path"])

    def test_create_re_attaches_when_path_missing_but_branch_survives(
        self, tmp_path, monkeypatch
    ):
        """Path-gone, branch-intact: delete the worktree dir (and git's pointer)
        but keep the branch + the tusk registry row. Re-running create must
        re-attach the worktree at the recorded path and report created:true.
        """
        repo, db_path, env, task_id, payload = self._make_recorded_workspace(
            tmp_path, monkeypatch, slug="reattach"
        )
        workspace_root = tmp_path / "workspaces"
        # Remove the worktree from disk AND from git's worktree registry, but
        # keep the branch and the tusk task_workspaces row intact.
        _git(["worktree", "remove", "--force", payload["workspace_path"]], cwd=repo)
        assert not os.path.isdir(payload["workspace_path"])
        # Confirm the branch still exists.
        branch_check = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet",
             f"refs/heads/{payload['branch']}"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert branch_check.returncode == 0
        # tusk task_workspaces row must still be there.
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, branch, workspace_path FROM task_workspaces "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        assert len(rows) == 1

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "reattach",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, (
            f"expected exit 0 from re-attach; got {result.returncode}\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        body = json.loads(result.stdout)
        assert body["created"] is True, (
            "Re-attach must report created:true so the caller knows the workspace was materialized"
        )
        assert body["workspace_path"] == payload["workspace_path"]
        assert os.path.isdir(body["workspace_path"])
        # The registry row must still be the same one (no duplicate INSERT).
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM task_workspaces WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == payload["workspace_id"]

    def test_create_refuses_when_path_and_branch_are_both_gone(
        self, tmp_path, monkeypatch
    ):
        """Path-gone, branch-also-gone: refuse with exit 2 and a diagnostic
        pointing at `tusk task-worktree prune` for cleanup.
        """
        repo, db_path, env, task_id, payload = self._make_recorded_workspace(
            tmp_path, monkeypatch, slug="fully-stale"
        )
        workspace_root = tmp_path / "workspaces"
        # Remove the worktree from disk, then also delete the branch.
        _git(["worktree", "remove", "--force", payload["workspace_path"]], cwd=repo)
        _git(["branch", "-D", payload["branch"]], cwd=repo)
        # Confirm branch is gone.
        branch_check = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet",
             f"refs/heads/{payload['branch']}"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert branch_check.returncode != 0

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "fully-stale",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 2, (
            f"expected exit 2 for fully stale row; got {result.returncode}\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        assert "tusk task-worktree prune" in result.stderr
        assert payload["branch"] in result.stderr
        assert payload["workspace_path"] in result.stderr
        # Registry row must NOT have been mutated by the failed call.
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM task_workspaces WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == payload["workspace_id"]


class TestTaskWorktreeCreateConfigOverride:
    """Issue #874 — `tusk task-worktree create --config <path>` overrides
    PROJECT_CONFIG so a feature branch that modifies dispatcher-consumed config
    keys (worktree.symlink_files, etc.) can be end-to-end verified before
    merge, instead of forcing operators to edit the primary config in place or
    import the helper via Python.
    """

    def _write_config(self, path, names):
        cfg = {"worktree": {"symlink_files": list(names)}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def _plant(self, repo, rel, content):
        full = os.path.join(str(repo), rel)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def test_config_override_honored_for_symlink_files(
        self, tmp_path, monkeypatch
    ):
        """When --config points at a config with a custom symlink_files list,
        the worktree gets symlinks for THAT list — not the primary's.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        # Pin TUSK_NO_AUTO_SYMLINK=1 so the canonical fallback can't bleed in
        # and create a false positive (issue #854 fallback would also create
        # .venv / .env links if either basename existed in the primary).
        env = dict(env)
        env["TUSK_NO_AUTO_SYMLINK"] = "1"

        # The primary's config is whatever `tusk init` produced (empty
        # worktree.symlink_files). Plant a uniquely-named marker file in the
        # primary so we know the symlink came from the override path.
        marker = ".custom-override-marker"
        self._plant(repo, marker, "override-evidence")

        override_path = tmp_path / "override-config.json"
        self._write_config(str(override_path), [marker])

        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "with-override",
                "--workspace-root",
                str(workspace_root),
                "--config",
                str(override_path),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt_marker = os.path.join(payload["workspace_path"], marker)
        assert os.path.islink(wt_marker), (
            f"expected --config override to create {marker} symlink in the "
            f"worktree; got: {os.listdir(payload['workspace_path'])}"
        )
        # Symlink must point at the primary's marker (absolute target).
        assert os.readlink(wt_marker) == os.path.join(str(repo), marker)

    def test_without_config_flag_behavior_unchanged(self, tmp_path, monkeypatch):
        """Without --config, dispatcher's resolve_config (primary) is used. The
        primary's empty worktree.symlink_files plus TUSK_NO_AUTO_SYMLINK=1
        means no symlinks should appear for the override-only marker file.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        env = dict(env)
        env["TUSK_NO_AUTO_SYMLINK"] = "1"

        marker = ".custom-override-marker"
        self._plant(repo, marker, "primary-only")

        # An override config exists on disk but is NOT passed via --config —
        # the primary's empty config should win.
        override_path = tmp_path / "override-config.json"
        self._write_config(str(override_path), [marker])

        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "no-override",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        wt_marker = os.path.join(payload["workspace_path"], marker)
        assert not os.path.lexists(wt_marker), (
            f"expected NO symlink without --config; found {wt_marker}"
        )

    def test_config_override_rejects_missing_path(self, tmp_path, monkeypatch):
        """A missing/unreadable --config path must error clearly naming the
        path, not silently fall back to the primary config.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_task(db_path)
        workspace_root = tmp_path / "workspaces"

        bogus_path = tmp_path / "does-not-exist.json"
        assert not bogus_path.exists()

        result = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "rejects-missing",
                "--workspace-root",
                str(workspace_root),
                "--config",
                str(bogus_path),
            ],
            cwd=repo,
            env=env,
        )

        assert result.returncode != 0, (
            "expected non-zero exit when --config path is missing; "
            f"got 0 with stdout: {result.stdout!r} stderr: {result.stderr!r}"
        )
        assert str(bogus_path) in result.stderr, (
            f"expected error message to name the missing path; got: {result.stderr!r}"
        )
        # Registry must not have an orphan row for this failed attempt.
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM task_workspaces WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        assert rows == []

    def test_config_override_help_text_documents_flag(self, tmp_path, monkeypatch):
        """Argparse --help must mention --config so operators discover it."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        result = _run(
            ["task-worktree", "create", "--help"],
            cwd=repo,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert "--config" in result.stdout, (
            f"expected --config in help output; got: {result.stdout!r}"
        )
        # Help must hint at the pre-merge verification use case so the flag's
        # purpose is discoverable without reading the issue body.
        assert "verify" in result.stdout.lower() or "pre-merge" in result.stdout.lower()

"""Regression test for `tusk review begin` invoked from a task-owned worktree.

Originally filed as TASK-368 after a /review-commits run from a
`tusk task-worktree create` workspace failed with
"Database error: attempt to write a readonly database". The path-resolution
fixes for issues #730/#731/#740 (TASK-373's def7b0b) resolved the underlying
symptom, but no integration test was exercising the full `tusk review begin`
write path from the worktree CWD — so a future regression would not have
been caught. This test closes that gap by running the real CLI against a
real task workspace and asserting that the code_reviews row is written.
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


def _git(args, *, cwd, env=None):
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


def _insert_in_progress_task(db_path, summary="review begin worktree task"):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score, started_at) "
            "VALUES (?, ?, 'In Progress', 'feature', 'High', 'M', 30, datetime('now'))",
            (summary, "exercise review begin from a task workspace"),
        )
        conn.commit()
        return cur.lastrowid


class TestReviewBeginFromTaskWorktree:
    def test_review_begin_writes_from_task_workspace(self, tmp_path, monkeypatch):
        """`tusk review begin <id>` invoked with cwd=task-workspace must
        succeed and persist a code_reviews row in the originating DB.

        Regression coverage for TASK-368: the same call previously surfaced
        "Database error: attempt to write a readonly database" when the
        worktree's CWD confused the diff-range or DB resolution paths.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)

        workspace_root = tmp_path / "workspaces"
        created = _run(
            [
                "task-worktree",
                "create",
                str(task_id),
                "review-begin-readonly",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=repo,
            env=env,
        )
        assert created.returncode == 0, created.stderr
        workspace = json.loads(created.stdout)["workspace_path"]

        # Land a real [TASK-N] commit inside the workspace so compute_range()
        # returns a non-empty diff and cmd_begin proceeds to the DB writes.
        worktree_file = os.path.join(workspace, "worktree-edit.txt")
        with open(worktree_file, "w", encoding="utf-8") as f:
            f.write("worktree change\n")
        _git(["add", "worktree-edit.txt"], cwd=workspace, env=env)
        _git(
            ["commit", "-m", f"[TASK-{task_id}] worktree change"],
            cwd=workspace,
            env=env,
        )

        result = _run(["review", "begin", str(task_id)], cwd=workspace, env=env)

        assert result.returncode == 0, (
            f"review begin from worktree failed\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        payload = json.loads(result.stdout)
        assert payload["task_id"] == task_id
        assert payload["review_id"]
        assert payload["diff_lines"] > 0

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, task_id, status FROM code_reviews WHERE id = ?",
                (payload["review_id"],),
            ).fetchone()
        assert row is not None, "code_reviews row missing — write did not land"
        assert row[1] == task_id
        assert row[2] == "pending"

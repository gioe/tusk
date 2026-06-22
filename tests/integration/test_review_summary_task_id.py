"""Integration tests for ``tusk review summary <task_id>`` (issue #1033).

``tusk review summary`` used to take a *review_id* while its sibling display
subcommands (``review list``/``status``/``verdict``) all take a *task_id*.
Passing a task_id silently resolved a colliding ``code_reviews.id`` belonging
to an unrelated task and rendered it with no ownership validation — the
original incident rendered task #1044's review when the operator asked for
task #2729's summary.

These tests exercise the real CLI against a real DB + git repo, mirroring the
``test_review_validate_comments.py`` integration-test pattern. They seed an
explicit id collision (a review whose id equals an unrelated task's id) so a
future regression of the arg wiring fails loudly.
"""

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


def _seed(db_path, tasks, reviews):
    """Seed tasks and code_reviews with explicit ids.

    tasks:   list of (id, summary)
    reviews: list of (id, task_id, status)
    """
    with sqlite3.connect(db_path) as conn:
        for tid, summary in tasks:
            conn.execute(
                "INSERT INTO tasks (id, summary, description, status, task_type, "
                "priority, complexity, priority_score) "
                "VALUES (?, ?, '', 'Done', 'feature', 'High', 'M', 30)",
                (tid, summary),
            )
        for rid, task_id, status in reviews:
            conn.execute(
                "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass) "
                "VALUES (?, ?, 'alice', ?, 1)",
                (rid, task_id, status),
            )
        conn.commit()


class TestReviewSummaryTaskId:
    def test_arg_is_task_id_not_review_id_under_collision(self, tmp_path, monkeypatch):
        """The arg must be a task_id. Reproduces issue #1033's collision: a
        review whose id equals an unrelated task's id must never be rendered
        in place of the queried task's own review."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        # Task 100 owns review 200. Task 200 owns review 999 (the collision:
        # review 200 belongs to task 100, but 200 is also a valid task_id).
        _seed(
            db_path,
            tasks=[(100, "task one hundred"), (200, "task two hundred")],
            reviews=[(200, 100, "approved"), (999, 200, "approved")],
        )

        result = _run(["review", "summary", "200"], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        # Resolves task 200's own review (#999), NOT review #200 (task 100).
        assert "Review #999 Summary" in result.stdout
        assert "Task:     #200 task two hundred" in result.stdout
        # The unrelated task must not leak in.
        assert "#100" not in result.stdout
        assert "task one hundred" not in result.stdout

    def test_latest_review_rendered_for_task(self, tmp_path, monkeypatch):
        """When a task has several reviews, the latest non-superseded one wins."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        _seed(
            db_path,
            tasks=[(50, "multi-review task")],
            reviews=[(10, 50, "changes_requested"), (20, 50, "approved")],
        )

        result = _run(["review", "summary", "50"], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        assert "Review #20 Summary" in result.stdout
        assert "Task:     #50 multi-review task" in result.stdout

    def test_task_with_no_reviews(self, tmp_path, monkeypatch):
        """A task that exists but has no review prints 'No reviews for task #N'
        rather than rendering an unrelated review."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        _seed(db_path, tasks=[(7, "unreviewed task")], reviews=[])

        result = _run(["review", "summary", "7"], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        assert "No reviews for task #7: unreviewed task" in result.stdout

    def test_nonexistent_task_errors(self, tmp_path, monkeypatch):
        """An unknown task_id fails loudly with exit 2."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        _seed(db_path, tasks=[], reviews=[])

        result = _run(["review", "summary", "999999"], cwd=repo, env=env)
        assert result.returncode == 2
        assert "Task 999999 not found" in result.stderr

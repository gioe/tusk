"""Regression coverage for the committed diff used by a second review pass."""

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
    return result.stdout


def _repo_with_task(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "reviewed.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "reviewed.txt"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)

    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    monkeypatch.setenv("TUSK_DB", str(db_path))
    monkeypatch.setenv("TUSK_QUIET", "1")

    initialized = _run(["init", "--force", "--skip-gitignore"], cwd=repo, env=env)
    assert initialized.returncode == 0, initialized.stderr

    with sqlite3.connect(db_path) as conn:
        task_id = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score, started_at) "
            "VALUES (?, ?, 'In Progress', 'feature', 'High', 'S', 30, datetime('now'))",
            ("second pass sees fixes", "review range regression"),
        ).lastrowid
        conn.commit()

    _git(["checkout", "-b", f"feature/TASK-{task_id}-review-fix"], cwd=repo)
    (repo / "implementation.txt").write_text("implementation\n", encoding="utf-8")
    _git(["add", "implementation.txt"], cwd=repo)
    _git(["commit", "-m", f"[TASK-{task_id}] implementation"], cwd=repo)
    return repo, env, task_id


def test_second_pass_diff_includes_uncommitted_review_fixes(tmp_path, monkeypatch):
    repo, env, task_id = _repo_with_task(tmp_path, monkeypatch)

    first = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
    assert first.returncode == 0, first.stderr

    (repo / "reviewed.txt").write_text("fixed in review\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("leave me alone\n", encoding="utf-8")

    review_fix_files = ["reviewed.txt"]
    _git(["add", "--", *review_fix_files], cwd=repo)
    _git(
        [
            "commit",
            "-m",
            f"[TASK-{task_id}] Apply review fixes",
            "--",
            *review_fix_files,
        ],
        cwd=repo,
    )

    second = _run(
        ["review", "begin", str(task_id), "--pass-num", "2"],
        cwd=repo,
        env=env,
    )
    assert second.returncode == 0, second.stderr
    diff_range = json.loads(second.stdout)["range"]
    second_pass_diff = _git(["diff", diff_range], cwd=repo)

    assert "fixed in review" in second_pass_diff
    assert _git(
        ["show", "--pretty=format:", "--name-only", "HEAD"], cwd=repo
    ).splitlines() == ["reviewed.txt"]
    assert "unrelated.txt" not in second_pass_diff
    assert _git(["diff", "--cached", "--name-only"], cwd=repo) == ""
    assert "?? unrelated.txt" in _git(["status", "--short"], cwd=repo)

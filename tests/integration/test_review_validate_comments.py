"""Integration tests for ``tusk review validate-comments`` (issue #783).

The reviewer agent has been observed fabricating findings that reference
files outside the actual diff — the orchestrator-side validator built in
TASK-393 enforces an objective ground truth: any pending comment whose
``file_path`` is not in ``git diff --name-only <range>`` is auto-resolved
as ``dismissed`` with an explanatory ``resolution_note``.

These tests exercise the real CLI against a real DB + git repo, mirroring
the ``test_review_begin_worktree.py`` integration-test pattern.
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


def _insert_in_progress_task(db_path, summary="validate-comments task"):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score, started_at) "
            "VALUES (?, ?, 'In Progress', 'feature', 'High', 'M', 30, datetime('now'))",
            (summary, "exercise reviewer-comment fabrication guard"),
        )
        conn.commit()
        return cur.lastrowid


class TestValidateComments:
    def test_dismisses_fabricated_file_path(self, tmp_path, monkeypatch):
        """A pending comment whose file_path is not in the diff must be
        auto-dismissed with a resolution_note that names both the offending
        path and the diff range. The comment's resolution becomes
        ``dismissed`` and persists across CLI invocations."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)

        # Create a feature branch with a real commit touching real.py only.
        _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], cwd=repo)
        (repo / "real.py").write_text("def real():\n    return 1\n", encoding="utf-8")
        _git(["add", "real.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] real"], cwd=repo, env=env)

        # tusk review begin → creates a code_reviews row, returns review_id.
        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        # Add three comments: one for a real file, one for a fabricated
        # file, and one general (file_path NULL).
        for args in [
            ["review", "add-comment", str(review_id), "real issue", "--file", "real.py", "--line-start", "1", "--category", "must_fix", "--severity", "minor"],
            ["review", "add-comment", str(review_id), "fabricated", "--file", "src/never_existed.py", "--line-start", "42", "--category", "must_fix", "--severity", "minor"],
            ["review", "add-comment", str(review_id), "general remark", "--category", "suggest", "--severity", "minor"],
        ]:
            r = _run(args, cwd=repo, env=env)
            assert r.returncode == 0, r.stderr

        # Validate comments.
        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["review_id"] == review_id
        assert payload["validated"] == 3
        assert payload["in_diff"] == 1
        assert payload["general"] == 1
        assert len(payload["dismissed"]) == 1
        assert payload["dismissed"][0]["file_path"] == "src/never_existed.py"
        assert "real.py" in payload["diff_files"]

        # Confirm the dismissal landed in the DB with a non-empty note.
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT file_path, resolution, resolution_note FROM review_comments"
                " WHERE review_id = ? ORDER BY id",
                (review_id,),
            ).fetchall()

        kept_real = next(r for r in rows if r["file_path"] == "real.py")
        dismissed = next(r for r in rows if r["file_path"] == "src/never_existed.py")
        general = next(r for r in rows if r["file_path"] is None)

        assert kept_real["resolution"] is None, "real-file comment must be untouched"
        assert dismissed["resolution"] == "dismissed"
        assert "src/never_existed.py" in (dismissed["resolution_note"] or "")
        assert "issue #783" in (dismissed["resolution_note"] or "")
        assert general["resolution"] is None, "general (file_path=null) must be untouched"

    def test_no_dismissals_when_every_path_in_diff(self, tmp_path, monkeypatch):
        """All file_paths in the diff → zero dismissals; comments stay open."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)

        _git(["checkout", "-b", f"feature/TASK-{task_id}-clean"], cwd=repo)
        (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
        _git(["add", "a.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] add a"], cwd=repo, env=env)

        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        r = _run(
            ["review", "add-comment", str(review_id), "real", "--file", "a.py", "--line-start", "1", "--category", "suggest", "--severity", "minor"],
            cwd=repo,
            env=env,
        )
        assert r.returncode == 0, r.stderr

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["dismissed"] == []
        assert payload["in_diff"] == 1
        assert payload["validated"] == 1

    def test_unknown_review_id_exits_two(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        result = _run(["review", "validate-comments", "99999"], cwd=repo, env=env)
        assert result.returncode == 2
        assert "Review 99999 not found" in result.stderr

"""Integration tests for tusk progress checkpoint metadata."""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(cmd: list[str], *, cwd, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(cmd)}\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )
    return result


def _init_repo(repo):
    _run(["git", "init", "-q", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)


def _commit(repo, filename: str, content: str, message: str) -> str:
    path = repo / filename
    path.write_text(content, encoding="utf-8")
    _run(["git", "add", filename], cwd=repo)
    _run(["git", "commit", "-q", "-m", message], cwd=repo)
    return _run(["git", "rev-parse", "--short", "HEAD"], cwd=repo).stdout.strip()


def _insert_task(db_path, *, status: str = "In Progress") -> int:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score)"
            " VALUES ('progress metadata task', ?, 'bug', 'High', 'S', 50)",
            (status,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _latest_progress(db_path, task_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT commit_hash, commit_message, files_changed, note, next_steps "
            "FROM task_progress WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()


def test_progress_leaves_commit_metadata_null_when_head_does_not_belong_to_task(db_path, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit(repo, "unrelated.txt", "unrelated\n", "[TASK-9999] unrelated work")
    task_id = _insert_task(db_path)

    result = _run(
        [TUSK_BIN, "progress", str(task_id), "--next-steps", "verification only"],
        cwd=repo,
    )

    payload = json.loads(result.stdout)
    assert payload["commit_hash"] is None
    assert payload["commit_message"] is None
    assert payload["files_changed"] is None
    row = _latest_progress(db_path, task_id)
    assert row["commit_hash"] is None
    assert row["commit_message"] is None
    assert row["files_changed"] is None
    assert row["note"] is None
    assert row["next_steps"] == "verification only"


def test_progress_records_commit_metadata_when_head_belongs_to_task(db_path, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit(repo, "seed.txt", "seed\n", "seed")
    task_id = _insert_task(db_path)
    commit_hash = _commit(
        repo,
        "feature.txt",
        "implemented\n",
        f"[TASK-{task_id}] implement progress metadata",
    )

    result = _run(
        [TUSK_BIN, "progress", str(task_id), "--next-steps", "ready for review"],
        cwd=repo,
    )

    payload = json.loads(result.stdout)
    assert payload["commit_hash"] == commit_hash
    assert payload["commit_message"] == f"[TASK-{task_id}] implement progress metadata"
    assert payload["files_changed"] == "feature.txt"
    row = _latest_progress(db_path, task_id)
    assert row["commit_hash"] == commit_hash
    assert row["commit_message"] == f"[TASK-{task_id}] implement progress metadata"
    assert row["files_changed"] == "feature.txt"
    assert row["note"] is None
    assert row["next_steps"] == "ready for review"


def test_progress_records_note_separately_from_next_steps(db_path, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    task_id = _insert_task(db_path)
    _commit(repo, "feature.txt", "implemented\n", f"[TASK-{task_id}] implement progress note")

    result = _run(
        [
            TUSK_BIN,
            "progress",
            str(task_id),
            "--note",
            "Why we chose X",
            "--next-steps",
            "Implement X",
        ],
        cwd=repo,
    )

    payload = json.loads(result.stdout)
    assert payload["note"] == "Why we chose X"
    assert payload["next_steps"] == "Implement X"
    row = _latest_progress(db_path, task_id)
    assert row["note"] == "Why we chose X"
    assert row["next_steps"] == "Implement X"

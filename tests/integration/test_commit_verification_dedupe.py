"""End-to-end coverage for commit-scoped verification-spec deduplication."""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
TUSK_COMMIT_PY = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _git(repo, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return result


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tusk@example.test")
    _git(repo, "config", "user.name", "Tusk Tests")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_PROJECT"] = str(repo)
    env["TUSK_QUIET"] = "1"
    initialized = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert initialized.returncode == 0, initialized.stderr
    return repo, db_path, env


def test_commit_runs_identical_verification_once_and_completes_both(tmp_path):
    repo, db_path, env = _init_repo(tmp_path)
    branch = "feature/TASK-1-dedupe"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("work\n", encoding="utf-8")

    counter = repo / "verification-count"
    spec = (
        "python3 -c \"from pathlib import Path; "
        "p=Path('verification-count'); "
        "n=int(p.read_text()) if p.exists() else 0; "
        "p.write_text(str(n + 1))\""
    )
    with sqlite3.connect(db_path) as conn:
        task_id = conn.execute(
            "INSERT INTO tasks "
            "(summary, description, status, task_type, priority, complexity, "
            "priority_score, scope_enforced) VALUES "
            "('dedupe verification', 'desc', 'In Progress', 'feature', "
            "'Medium', 'S', 30, 1)"
        ).lastrowid
        criterion_ids = []
        for name in ("first", "second"):
            criterion_ids.append(
                conn.execute(
                    "INSERT INTO acceptance_criteria "
                    "(task_id, criterion, criterion_type, verification_spec) "
                    "VALUES (?, ?, 'test', ?)",
                    (task_id, name, spec),
                ).lastrowid
            )
        conn.execute(
            "INSERT INTO task_scope (task_id, pattern, source, reason) "
            "VALUES (?, 'feature.txt', 'operator_declared', 'test fixture')",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (?, ?, ?)",
            (task_id, branch, str(repo)),
        )
        conn.commit()

    config_path = repo / "commit-config.json"
    config_path.write_text(json.dumps({"test_command": None}), encoding="utf-8")
    result = subprocess.run(
        [
            "python3",
            TUSK_COMMIT_PY,
            str(repo),
            str(config_path),
            str(task_id),
            "dedupe exact verification specs",
            "feature.txt",
            "--criteria",
            *(str(cid) for cid in criterion_ids),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert counter.read_text(encoding="utf-8") == "1"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT is_completed, verification_result "
            "FROM acceptance_criteria ORDER BY id"
        ).fetchall()
    assert [row[0] for row in rows] == [1, 1]
    second_result = json.loads(rows[1][1])
    assert second_result["reused_commit_verification"] is True

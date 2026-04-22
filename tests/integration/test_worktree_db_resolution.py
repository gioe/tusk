"""Regression: linked git worktrees must reuse the shared tusk DB.

Reproduces issue #523: running `tusk` from a linked worktree used to resolve
`tusk/tasks.db` relative to the worktree checkout, so an empty local sqlite
file caused commands like `tusk criteria done` to fail with `no such table:
acceptance_criteria`. Linked worktrees should hit the main checkout's DB.
"""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(cmd, cwd, env=None, check=True):
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _git_init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main", str(repo)], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "seed"], cwd=repo)


def test_linked_worktree_reuses_primary_db(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)

    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)

    _run([TUSK_BIN, "init", "--yes"], cwd=repo, env=env)
    inserted = _run(
        [
            TUSK_BIN,
            "task-insert",
            "Worktree DB regression fixture",
            "Fixture task for linked worktree DB resolution coverage.",
            "--priority",
            "High",
            "--domain",
            "cli",
            "--task-type",
            "bug",
            "--complexity",
            "S",
            "--criteria",
            "Criterion should be completable from a linked worktree.",
        ],
        cwd=repo,
        env=env,
    )
    task = json.loads(inserted.stdout)
    criterion_id = task["criteria_ids"][0]

    worktree = tmp_path / "repo-wt"
    _run(["git", "worktree", "add", "--detach", str(worktree)], cwd=repo)
    nested = worktree / "nested" / "dir"
    nested.mkdir(parents=True, exist_ok=True)

    # Mimic the bad state from issue #523: a local empty DB inside the linked
    # worktree must be ignored in favor of the primary checkout's shared DB.
    (worktree / "tusk").mkdir(exist_ok=True)
    sqlite3.connect(worktree / "tusk" / "tasks.db").close()

    path = _run([TUSK_BIN, "path"], cwd=nested, env=env)
    assert path.stdout.strip() == str(repo / "tusk" / "tasks.db")

    done = _run(
        [TUSK_BIN, "criteria", "done", str(criterion_id), "--skip-verify"],
        cwd=nested,
        env=env,
    )
    assert done.returncode == 0, done.stderr

    db = sqlite3.connect(repo / "tusk" / "tasks.db")
    try:
        is_completed = db.execute(
            "SELECT is_completed FROM acceptance_criteria WHERE id = ?",
            (criterion_id,),
        ).fetchone()[0]
    finally:
        db.close()
    assert is_completed == 1

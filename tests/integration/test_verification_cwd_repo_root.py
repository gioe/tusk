"""Regression: criterion verification specs run from the repo root, not caller cwd.

Issue #588: when ``tusk commit --criteria`` or ``tusk criteria done`` was
invoked from a subdirectory of the repo (common after a sub-skill ``cd``s
into ``ios/`` or similar and doesn't restore), code-type specs that used
repo-root-relative paths failed with "No such file or directory" because
``run_verification`` ran ``subprocess.run`` without a ``cwd=`` and ``glob``
resolved patterns against the caller's cwd.

The fix anchors both at ``git rev-parse --show-toplevel``. This test
exercises a code-type criterion whose spec checks for a marker file at the
repo root using a relative path, while the test process invokes
``tusk criteria done`` from a nested subdirectory of the repo.
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
        encoding="utf-8",
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


def test_code_criterion_verification_runs_from_repo_root(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    # Marker file at the repo root — the spec references it by repo-root-relative path.
    (repo / "marker.txt").write_text("present\n", encoding="utf-8")

    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)

    _run([TUSK_BIN, "init", "--yes"], cwd=repo, env=env)
    inserted = _run(
        [
            TUSK_BIN, "task-insert",
            "Verification cwd regression fixture",
            "Fixture task for repo-root-relative spec verification from a subdirectory.",
            "--priority", "High",
            "--domain", "cli",
            "--task-type", "bug",
            "--complexity", "S",
            "--criteria", "Marker file exists at repo root.",
        ],
        cwd=repo,
        env=env,
    )
    task = json.loads(inserted.stdout)
    task_id = task["task_id"]

    added = _run(
        [
            TUSK_BIN, "criteria", "add", str(task_id),
            "Repo-root-relative spec resolves correctly from subdirectory",
            "--type", "code",
            "--spec", "test -f marker.txt",
        ],
        cwd=repo,
        env=env,
    )
    criterion_id = json.loads(added.stdout)["id"]

    # Invoke from a nested subdirectory — this is the scenario from issue #588.
    nested = repo / "ios" / "AIQ" / "Features"
    nested.mkdir(parents=True, exist_ok=True)
    done = _run(
        [TUSK_BIN, "criteria", "done", str(criterion_id)],
        cwd=nested,
        env=env,
        check=False,
    )
    assert done.returncode == 0, (
        f"criteria done from {nested} should resolve repo-root-relative "
        f"spec via repo root anchoring.\nstdout: {done.stdout}\nstderr: {done.stderr}"
    )

    db = sqlite3.connect(repo / "tusk" / "tasks.db")
    try:
        is_completed = db.execute(
            "SELECT is_completed FROM acceptance_criteria WHERE id = ?",
            (criterion_id,),
        ).fetchone()[0]
    finally:
        db.close()
    assert is_completed == 1


def test_file_criterion_verification_resolves_glob_from_repo_root(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / "ios").mkdir()
    target = repo / "ios" / "Foo.swift"
    target.write_text("// swift\n", encoding="utf-8")

    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)

    _run([TUSK_BIN, "init", "--yes"], cwd=repo, env=env)
    inserted = _run(
        [
            TUSK_BIN, "task-insert",
            "File-type verification cwd fixture",
            "Fixture task for file-type glob anchoring at repo root.",
            "--priority", "Medium",
            "--domain", "cli",
            "--task-type", "bug",
            "--complexity", "S",
            "--criteria", "Glob anchored at repo root.",
        ],
        cwd=repo,
        env=env,
    )
    task_id = json.loads(inserted.stdout)["task_id"]
    added = _run(
        [
            TUSK_BIN, "criteria", "add", str(task_id),
            "File pattern resolves from repo root",
            "--type", "file",
            "--spec", "ios/*.swift",
        ],
        cwd=repo,
        env=env,
    )
    criterion_id = json.loads(added.stdout)["id"]

    # Drop into a deep subdirectory and verify the glob still resolves.
    nested = repo / "ios" / "AIQ" / "Features"
    nested.mkdir(parents=True, exist_ok=True)
    done = _run(
        [TUSK_BIN, "criteria", "done", str(criterion_id)],
        cwd=nested,
        env=env,
        check=False,
    )
    assert done.returncode == 0, (
        f"file-type criteria done from {nested} should resolve "
        f"repo-root-relative glob.\nstdout: {done.stdout}\nstderr: {done.stderr}"
    )

    db = sqlite3.connect(repo / "tusk" / "tasks.db")
    try:
        is_completed = db.execute(
            "SELECT is_completed FROM acceptance_criteria WHERE id = ?",
            (criterion_id,),
        ).fetchone()[0]
    finally:
        db.close()
    assert is_completed == 1

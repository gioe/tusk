"""Regression coverage for issue #1140's task-start schema guard.

When a live DB is ahead of the installed binary's schema support, ``task-start``
must fail in the bash preflight before it mutates task/session/skill-run state.
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate_task_start_preflight",
        os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _supported_schema_max() -> int:
    return max(v for v, _ in _load_migrate().MIGRATIONS)


def _counts(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            "task_sessions": conn.execute("SELECT COUNT(*) FROM task_sessions").fetchone()[0],
            "skill_runs": conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0],
        }
    finally:
        conn.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["task-start", "1", "--force", "--skill", "tusk"],
        ["task-brief", "1"],
        ["task-worktree", "create", "1", "skewed-task"],
    ],
)
def test_schema_mismatch_blocks_task_startup_subcommands_before_mutation(db_path, argv):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("INSERT INTO tasks (id, summary, status) VALUES (1, 'skewed task', 'To Do')")
        conn.execute(f"PRAGMA user_version = {_supported_schema_max() + 1}")
        conn.commit()
    finally:
        conn.close()

    before = _counts(db_path)
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"

    result = subprocess.run(
        [TUSK_BIN, *argv],
        capture_output=True,
        text=True,
        env=env,
    )

    after = _counts(db_path)
    assert result.returncode != 0
    assert "Schema mismatch" in result.stderr
    assert after == before

"""Wrapper-level regression tests for the two accepted ``tusk jot`` forms."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [TUSK_BIN, *argv], capture_output=True, text=True, cwd=REPO_ROOT
    )


def _seed_open_skill_run(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = conn.execute(
            "INSERT INTO tasks "
            "(summary, status, task_type, priority, complexity, priority_score) "
            "VALUES ('jot dispatch host', 'In Progress', 'bug', 'Medium', 'S', 50)"
        ).lastrowid
        conn.execute(
            "INSERT INTO skill_runs (skill_name, task_id) VALUES ('tusk', ?)",
            (task_id,),
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_parallel_runs(db_path, *, map_first_to_caller=False):
    conn = sqlite3.connect(str(db_path))
    try:
        first_task = conn.execute(
            "INSERT INTO tasks "
            "(summary, status, task_type, priority, complexity, priority_score) "
            "VALUES ('first jot task', 'In Progress', 'bug', 'Medium', 'S', 50)"
        ).lastrowid
        second_task = conn.execute(
            "INSERT INTO tasks "
            "(summary, status, task_type, priority, complexity, priority_score) "
            "VALUES ('second jot task', 'In Progress', 'bug', 'Medium', 'S', 50)"
        ).lastrowid
        first_run = conn.execute(
            "INSERT INTO skill_runs (skill_name, task_id, started_at) "
            "VALUES ('tusk', ?, '2026-01-01 00:00:00')",
            (first_task,),
        ).lastrowid
        second_run = conn.execute(
            "INSERT INTO skill_runs (skill_name, task_id, started_at) "
            "VALUES ('tusk', ?, '2026-01-01 00:01:00')",
            (second_task,),
        ).lastrowid
        if map_first_to_caller:
            conn.execute(
                "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
                "VALUES (?, ?, ?)",
                (first_task, f"feature/TASK-{first_task}-jot-test", REPO_ROOT),
            )
        conn.commit()
        return first_task, first_run, second_task, second_run
    finally:
        conn.close()


def test_explicit_write_and_shorthand_store_and_list_the_same_fields(db_path):
    task_id = _seed_open_skill_run(db_path)

    explicit = _run("jot", "write", "workflow", "explicit note")
    assert explicit.returncode == 0, explicit.stderr
    assert json.loads(explicit.stdout)["category"] == "workflow"
    assert json.loads(explicit.stdout)["note"] == "explicit note"

    shorthand = _run("jot", "process", "shorthand note")
    assert shorthand.returncode == 0, shorthand.stderr
    assert json.loads(shorthand.stdout)["category"] == "process"
    assert json.loads(shorthand.stdout)["note"] == "shorthand note"

    listed = _run("jots", "--task-id", str(task_id))
    assert listed.returncode == 0, listed.stderr
    assert [
        (row["category"], row["note"]) for row in json.loads(listed.stdout)
    ] == [
        ("process", "shorthand note"),
        ("workflow", "explicit note"),
    ]


def test_write_remains_available_as_a_legacy_category(db_path):
    task_id = _seed_open_skill_run(db_path)

    result = _run(
        "jot", "write", "category named write", "--file", "bin/tusk",
        "--task-id", str(task_id),
    )

    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row["category"] == "write"
    assert row["note"] == "category named write"
    assert row["file_hint"] == "bin/tusk"


def test_jot_help_advertises_the_explicit_write_form(db_path):
    result = _run("jot", "--help")

    assert result.returncode == 0, result.stderr
    assert "usage: tusk jot write" in result.stdout
    assert "--task-id TASK_ID" in result.stdout
    assert "--skill-run-id SKILL_RUN_ID" in result.stdout


def test_caller_workspace_targets_older_parallel_task(db_path):
    first_task, first_run, _, _ = _seed_parallel_runs(
        db_path, map_first_to_caller=True
    )

    result = _run("jot", "write", "process", "belongs to first task")

    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row["task_id"] == first_task
    assert row["skill_run_id"] == first_run


def test_unmapped_parallel_runs_refuse_without_inserting(db_path):
    _seed_parallel_runs(db_path)

    result = _run("jot", "write", "process", "must not be stored")

    assert result.returncode == 1
    assert "Ambiguous jot target" in result.stderr
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM jots").fetchone()[0] == 0
    finally:
        conn.close()


def test_explicit_skill_run_targets_requested_parallel_run(db_path):
    first_task, first_run, _, _ = _seed_parallel_runs(db_path)

    result = _run(
        "jot", "write", "process", "belongs to first run",
        "--skill-run-id", str(first_run),
    )

    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row["task_id"] == first_task
    assert row["skill_run_id"] == first_run

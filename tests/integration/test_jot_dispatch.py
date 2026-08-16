"""Wrapper-level regression tests for the two accepted ``tusk jot`` forms."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([TUSK_BIN, *argv], capture_output=True, text=True)


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
    _seed_open_skill_run(db_path)

    result = _run(
        "jot", "write", "category named write", "--file", "bin/tusk"
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

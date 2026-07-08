"""Regression tests for task-insert typed criteria handling."""

from __future__ import annotations

import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK = os.path.join(REPO_ROOT, "bin", "tusk")


@pytest.fixture()
def tusk_db(tmp_path):
    db_path = tmp_path / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    result = subprocess.run(
        [TUSK, "init", "--force"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return env


def test_task_insert_rejects_pipe_delimited_typed_criteria(tusk_db):
    result = subprocess.run(
        [
            TUSK,
            "task-insert",
            "repro",
            "repro",
            "--criteria",
            "file exists|code|test -f README.md",
        ],
        cwd=REPO_ROOT,
        env=tusk_db,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode != 0
    assert "--criteria does not accept pipe-delimited typed criteria" in result.stderr
    assert "--typed-criteria" in result.stderr


def test_task_insert_preserves_ordinary_manual_criteria_with_pipe(tusk_db):
    result = subprocess.run(
        [
            TUSK,
            "task-insert",
            "manual pipe",
            "manual pipe",
            "--criteria",
            "Document the A | B tradeoff",
        ],
        cwd=REPO_ROOT,
        env=tusk_db,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    rows = subprocess.run(
        [
            TUSK,
            "-json",
            "SELECT criterion, criterion_type, verification_spec FROM acceptance_criteria",
        ],
        cwd=REPO_ROOT,
        env=tusk_db,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rows.returncode == 0, rows.stderr
    assert '"criterion":"Document the A | B tradeoff"' in rows.stdout
    assert '"criterion_type":"manual"' in rows.stdout
    assert '"verification_spec":null' in rows.stdout


def test_task_insert_typed_criteria_json_still_records_verification_spec(tusk_db):
    result = subprocess.run(
        [
            TUSK,
            "task-insert",
            "typed json",
            "typed json",
            "--typed-criteria",
            '{"text":"file exists","type":"code","spec":"test -f README.md"}',
        ],
        cwd=REPO_ROOT,
        env=tusk_db,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    rows = subprocess.run(
        [
            TUSK,
            "-json",
            "SELECT criterion, criterion_type, verification_spec FROM acceptance_criteria",
        ],
        cwd=REPO_ROOT,
        env=tusk_db,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rows.returncode == 0, rows.stderr
    assert '"criterion":"file exists"' in rows.stdout
    assert '"criterion_type":"code"' in rows.stdout
    assert '"verification_spec":"test -f README.md"' in rows.stdout

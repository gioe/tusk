"""Regression coverage for ``tusk scope add`` under the silent-exit guard.

The issue reports came from the real CLI path: agent callers captured stderr,
``bin/tusk`` enabled its silent-exit guard, and ``tusk scope add`` surfaced only
the generic "no diagnostic output" footer. These tests exercise the dispatcher
path rather than only importing ``tusk-scope.py`` directly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TUSK_BIN = REPO_ROOT / "bin" / "tusk"


def _run(args: list[str], db_path: Path, *, env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env.pop("TUSK_GUARD_ACTIVE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(TUSK_BIN), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(REPO_ROOT),
    )


def _insert_task(db_path: Path) -> int:
    result = _run(
        [
            "task-insert",
            "scope silent guard regression",
            "body",
            "--criteria",
            "marker",
        ],
        db_path,
    )
    assert result.returncode == 0, result.stderr
    return int(json.loads(result.stdout)["task_id"])


def test_scope_add_valid_path_succeeds_under_normal_silent_exit_guard(db_path):
    task_id = _insert_task(db_path)

    result = _run(
        [
            "scope",
            "add",
            str(task_id),
            "bin/tusk-scope.py",
            "--reason",
            "regression",
        ],
        db_path,
    )

    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row["task_id"] == task_id
    assert row["pattern"] == "bin/tusk-scope.py"
    assert "no diagnostic output" not in result.stderr


def test_scope_add_locked_db_reports_actionable_error_not_silent_guard(db_path):
    task_id = _insert_task(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN EXCLUSIVE")

        result = _run(
            [
                "scope",
                "add",
                str(task_id),
                "bin/tusk-scope.py",
                "--reason",
                "regression",
            ],
            db_path,
            env_extra={"TUSK_BUSY_TIMEOUT_MS": "0"},
        )
    finally:
        conn.rollback()
        conn.close()

    assert result.returncode == 1
    assert "scope add crashed" in result.stderr
    assert "OperationalError" in result.stderr
    assert "database is locked" in result.stderr
    assert "no diagnostic output" not in result.stderr

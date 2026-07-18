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
import time
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


def _popen(args: list[str], db_path: Path, *, env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env.pop("TUSK_GUARD_ACTIVE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        [str(TUSK_BIN), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
            env_extra={
                "TUSK_BUSY_TIMEOUT_MS": "0",
                "TUSK_WRITE_RETRIES": "1",
                "TUSK_WRITE_RETRY_BASE_MS": "0",
                "TUSK_NO_BACKUP": "1",
            },
        )
    finally:
        conn.rollback()
        conn.close()

    assert result.returncode == 1
    assert result.stderr.count("database stayed locked after 2 attempts (scope add)") == 1
    assert "scope add crashed" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "no diagnostic output" not in result.stderr


def test_concurrent_scope_add_writers_recover_after_lock_clears(db_path):
    task_id = _insert_task(db_path)
    paths = [
        "bin/tusk-scope.py",
        "bin/tusk-db-lib.py",
        "tests/unit/test_write_retry.py",
        "tests/integration/test_scope_cli.py",
    ]
    retry_env = {
        "TUSK_BUSY_TIMEOUT_MS": "0",
        "TUSK_WRITE_RETRIES": "50",
        "TUSK_WRITE_RETRY_BASE_MS": "5",
        "TUSK_NO_BACKUP": "1",
    }
    holder = sqlite3.connect(str(db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    processes = [
        _popen(
            ["scope", "add", str(task_id), path, "--reason", "parallel retry"],
            db_path,
            env_extra=retry_env,
        )
        for path in paths
    ]
    try:
        time.sleep(0.2)
        holder.rollback()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            results.append((process.returncode, stdout, stderr))
    finally:
        if holder.in_transaction:
            holder.rollback()
        holder.close()

    assert all(rc == 0 for rc, _, _ in results), results
    conn = sqlite3.connect(str(db_path))
    stored = {
        row[0]
        for row in conn.execute(
            "SELECT pattern FROM task_scope WHERE task_id = ?", (task_id,)
        )
    }
    conn.close()
    assert stored == set(paths)


def test_concurrent_same_pattern_scope_add_is_idempotent(db_path):
    task_id = _insert_task(db_path)
    retry_env = {
        "TUSK_BUSY_TIMEOUT_MS": "0",
        "TUSK_WRITE_RETRIES": "50",
        "TUSK_WRITE_RETRY_BASE_MS": "5",
        "TUSK_NO_BACKUP": "1",
    }
    processes = [
        _popen(
            ["scope", "add", str(task_id), "bin/tusk-scope.py"],
            db_path,
            env_extra=retry_env,
        )
        for _ in range(4)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        results.append((process.returncode, stdout, stderr))

    assert all(rc == 0 for rc, _, _ in results), results
    payloads = [json.loads(stdout) for _, stdout, _ in results]
    assert len({payload["id"] for payload in payloads}) == 1
    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM task_scope WHERE task_id = ? AND pattern = ?",
        (task_id, "bin/tusk-scope.py"),
    ).fetchone()[0]
    conn.close()
    assert count == 1

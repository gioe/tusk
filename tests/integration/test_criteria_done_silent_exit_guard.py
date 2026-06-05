"""Regression coverage for parallel ``tusk criteria done`` diagnostics.

Issue #1022 reported several concurrent ``criteria done`` calls where one
process returned only bin/tusk's generic silent-exit guard footer. These tests
exercise the real dispatcher path with stderr captured, matching agent/CI
callers rather than importing ``tusk-criteria.py`` directly.
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


def _insert_task(db_path: Path) -> list[int]:
    result = _run(
        [
            "task-insert",
            "criteria done silent guard regression",
            "body",
            "--criteria",
            "first",
            "--criteria",
            "second",
            "--criteria",
            "third",
            "--criteria",
            "fourth",
        ],
        db_path,
    )
    assert result.returncode == 0, result.stderr
    return [int(cid) for cid in json.loads(result.stdout)["criteria_ids"]]


def test_parallel_criteria_done_locked_db_reports_actionable_errors(db_path):
    criterion_ids = _insert_task(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN EXCLUSIVE")

        env = os.environ.copy()
        env["TUSK_DB"] = str(db_path)
        env["TUSK_BUSY_TIMEOUT_MS"] = "0"
        env.pop("TUSK_GUARD_ACTIVE", None)
        procs = [
            subprocess.Popen(
                [str(TUSK_BIN), "criteria", "done", str(cid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=str(REPO_ROOT),
            )
            for cid in criterion_ids
        ]
        results = [proc.communicate(timeout=10) + (proc.returncode,) for proc in procs]
    finally:
        conn.rollback()
        conn.close()

    for stdout, stderr, returncode in results:
        assert returncode == 1
        assert stdout == ""
        assert "criteria done crashed with OperationalError" in stderr
        assert "database is locked" in stderr
        assert "no diagnostic output" not in stderr

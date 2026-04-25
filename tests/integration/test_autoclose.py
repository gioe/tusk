"""End-to-end coverage for `tusk autoclose [--dry-run]` on a populated DB.

Exists so the SELECT/UPDATE pair lives in exactly one place
(`bin/tusk-autoclose.py`) and the dry-run flag is exercised directly rather
than only through the `tusk groom` orchestrator.
"""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

WET_KEYS = {"applied", "expired_deferred", "moot_contingent", "total_closed"}


def _run_autoclose(db_path, *flags):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    return subprocess.run(
        [TUSK_BIN, "autoclose", *flags],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def _seed_expired_deferred(db_file):
    """Insert one deferred + expired To Do task. Returns its id."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, status, task_type, priority, complexity, "
            "priority_score, is_deferred, expires_at) "
            "VALUES (?, 'To Do', 'feature', 'Medium', 'S', 50, 1, '2000-01-01 00:00:00')",
            ("Old deferred spike",),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row_status(db_file, task_id):
    conn = sqlite3.connect(db_file)
    try:
        return conn.execute(
            "SELECT status, closed_reason FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()


class TestAutocloseDryRun:
    def test_dry_run_reports_candidates_without_closing(self, db_path):
        task_id = _seed_expired_deferred(str(db_path))
        result = _run_autoclose(db_path, "--dry-run")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["applied"] is False
        assert task_id in payload["expired_deferred"]["task_ids"]
        assert payload["expired_deferred"]["count"] >= 1
        assert payload["total_closed"] == payload["expired_deferred"]["count"] + payload[
            "moot_contingent"
        ]["count"]
        # moot_details is wet-run-only.
        assert "moot_details" not in payload

        assert _row_status(str(db_path), task_id) == ("To Do", None)

    def test_wet_run_closes_and_reports_applied_true(self, db_path):
        task_id = _seed_expired_deferred(str(db_path))
        result = _run_autoclose(db_path)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["applied"] is True
        assert task_id in payload["expired_deferred"]["task_ids"]
        assert _row_status(str(db_path), task_id) == ("Done", "expired")

    def test_dry_run_and_wet_run_select_same_candidates(self, db_path):
        task_id = _seed_expired_deferred(str(db_path))
        dry = json.loads(_run_autoclose(db_path, "--dry-run").stdout)
        # Wet-run is destructive; re-seed and capture.
        task_id2 = _seed_expired_deferred(str(db_path))
        wet = json.loads(_run_autoclose(db_path).stdout)
        assert task_id in dry["expired_deferred"]["task_ids"]
        # Wet-run includes both task_id (still open at SELECT time after dry-run
        # left it untouched) and task_id2.
        wet_ids = set(wet["expired_deferred"]["task_ids"])
        assert {task_id, task_id2} <= wet_ids

    def test_unknown_flag_rejected(self, db_path):
        result = _run_autoclose(db_path, "--bogus")
        assert result.returncode != 0
        assert "Unknown flag" in result.stderr or "Unknown flags" in result.stderr

    def test_wet_run_keys_match_documented_shape(self, db_path):
        _seed_expired_deferred(str(db_path))
        payload = json.loads(_run_autoclose(db_path).stdout)
        # moot_details is conditional; subtract it from the comparison.
        keys = set(payload.keys()) - {"moot_details"}
        assert keys == WET_KEYS

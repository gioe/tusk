"""Integration tests for `tusk task-insert --fixes-task-id <id>`.

Covers:
- happy path: the flag persists the value to tasks.fixes_task_id
- dangling reference: --fixes-task-id pointing at a non-existent task fails
- default: omitting the flag leaves fixes_task_id NULL
"""

import json
import os
import sqlite3
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _insert(db_path, *args):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    result = subprocess.run(
        [TUSK_BIN, "task-insert", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    return result


def _fixes(db_path, task_id):
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT fixes_task_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


class TestTaskInsertFixesTaskId:

    def test_persists_when_ref_exists(self, db_path):
        r1 = _insert(db_path, "Original work", "desc", "--criteria", "done")
        assert r1.returncode == 0, r1.stderr
        original_id = json.loads(r1.stdout)["task_id"]

        r2 = _insert(
            db_path,
            "Follow-up",
            "desc",
            "--criteria", "done",
            "--fixes-task-id", str(original_id),
        )
        assert r2.returncode == 0, r2.stderr
        followup_id = json.loads(r2.stdout)["task_id"]

        assert _fixes(db_path, followup_id) == original_id

    def test_omitted_flag_leaves_null(self, db_path):
        r = _insert(db_path, "Unrelated", "desc", "--criteria", "done")
        assert r.returncode == 0, r.stderr
        task_id = json.loads(r.stdout)["task_id"]

        assert _fixes(db_path, task_id) is None

    def test_dangling_reference_rejected(self, db_path):
        r = _insert(
            db_path,
            "Bad ref",
            "desc",
            "--criteria", "done",
            "--fixes-task-id", "999",
        )
        assert r.returncode == 2
        assert "does not reference an existing task" in r.stderr

    def test_persists_with_expires_in(self, db_path):
        """fixes_task_id must survive the expires-in INSERT branch too."""
        r1 = _insert(db_path, "Original", "desc", "--criteria", "done")
        original_id = json.loads(r1.stdout)["task_id"]

        r2 = _insert(
            db_path,
            "Follow-up w/ expiry",
            "desc",
            "--criteria", "done",
            "--fixes-task-id", str(original_id),
            "--expires-in", "30",
        )
        assert r2.returncode == 0, r2.stderr
        followup_id = json.loads(r2.stdout)["task_id"]

        assert _fixes(db_path, followup_id) == original_id

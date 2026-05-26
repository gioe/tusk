"""Integration tests for the ``tusk scope`` CLI surface and the
``tusk task-insert --scope/--creates/--unbounded`` flags.

Covers:
- ``scope add`` records the row with ``source='expanded_mid_task'`` and the
  reason text the operator passed (criterion 2183)
- ``scope list`` emits a JSON array of every entry for the task
- ``scope lock`` stamps ``locked_at`` and ``locked_by`` on previously
  unlocked rows and leaves already-locked rows alone
- ``task-insert --scope/--creates/--unbounded`` populate ``task_scope``
  with the correct source attribution (criterion 2198)
"""

import json
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _seed_task(db: str, summary: str = "scope-cli-test", description: str = "") -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO tasks (summary, description, task_type, priority, complexity, priority_score) "
        "VALUES (?, ?, 'feature', 'Medium', 'S', 10)",
        (summary, description),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id


def _run(args, env=None):
    """Invoke `tusk <args...>` and return the CompletedProcess."""
    result = subprocess.run(
        [TUSK_BIN, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return result


def _scope_rows(db: str, task_id: int) -> list:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT pattern, source, reason, locked_at, locked_by "
        "FROM task_scope WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── scope add ────────────────────────────────────────────────────────────────

class TestScopeAdd:

    def test_scope_add_logs_reason(self, db_path):
        """`tusk scope add` records the pattern with source='expanded_mid_task'
        and stamps the --reason text into the DB row (criterion 2183)."""
        task_id = _seed_task(str(db_path), description="add a helper to bin/")
        reason = "exploration revealed a missing helper not named in description"

        result = _run([
            "scope", "add", str(task_id),
            "bin/tusk-helper.py",
            "--reason", reason,
        ])
        assert result.returncode == 0, result.stderr

        payload = json.loads(result.stdout)
        assert payload["task_id"] == task_id
        assert payload["pattern"] == "bin/tusk-helper.py"
        assert payload["source"] == "expanded_mid_task"
        assert payload["reason"] == reason

        rows = _scope_rows(str(db_path), task_id)
        assert any(
            r["pattern"] == "bin/tusk-helper.py"
            and r["source"] == "expanded_mid_task"
            and r["reason"] == reason
            for r in rows
        ), f"reason missing from DB row: {rows}"

    def test_scope_add_rejects_unbounded_source(self, db_path):
        """`scope add` only accepts the mid-task source vocabulary —
        --source unbounded is reserved for `task-insert`."""
        task_id = _seed_task(str(db_path))
        result = _run([
            "scope", "add", str(task_id),
            "bin/foo.py",
            "--source", "unbounded",
        ])
        assert result.returncode != 0

    def test_scope_add_missing_task_errors(self, db_path):
        result = _run(["scope", "add", "999999", "bin/foo.py"])
        assert result.returncode != 0
        assert "not found" in result.stderr.lower()


# ── scope list ───────────────────────────────────────────────────────────────

class TestScopeList:

    def test_list_emits_json_array(self, db_path):
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/a.py"])
        _run(["scope", "add", str(task_id), "bin/b.py"])

        result = _run(["scope", "list", str(task_id)])
        assert result.returncode == 0
        rows = json.loads(result.stdout)
        assert isinstance(rows, list)
        patterns = sorted(r["pattern"] for r in rows)
        assert patterns == ["bin/a.py", "bin/b.py"]

    def test_list_empty_for_task_without_scope(self, db_path):
        task_id = _seed_task(str(db_path))
        result = _run(["scope", "list", str(task_id)])
        assert result.returncode == 0
        assert json.loads(result.stdout) == []


# ── scope lock ───────────────────────────────────────────────────────────────

class TestScopeLock:

    def test_lock_stamps_unlocked_rows(self, db_path):
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/a.py"])
        _run(["scope", "add", str(task_id), "bin/b.py"])

        result = _run(["scope", "lock", str(task_id), "--by", "tester"])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["rows_locked"] == 2
        assert payload["locked_by"] == "tester"

        for row in _scope_rows(str(db_path), task_id):
            assert row["locked_at"] is not None
            assert row["locked_by"] == "tester"

    def test_lock_is_idempotent_for_already_locked_rows(self, db_path):
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/a.py"])

        first = _run(["scope", "lock", str(task_id), "--by", "first"])
        assert json.loads(first.stdout)["rows_locked"] == 1

        second = _run(["scope", "lock", str(task_id), "--by", "second"])
        assert json.loads(second.stdout)["rows_locked"] == 0

        row = _scope_rows(str(db_path), task_id)[0]
        assert row["locked_by"] == "first", "already-locked rows must keep their original locked_by"


# ── task-insert flags (criterion 2198) ──────────────────────────────────────

class TestTaskInsertScopeFlags:

    def _insert_with_scope_flags(self, db: str, extra_args: list) -> int:
        result = _run([
            "task-insert",
            "scope-flags",
            "exercise task-insert scope flags",
            "--complexity", "S",
            "--criteria", "marker",
            *extra_args,
        ])
        assert result.returncode == 0, f"task-insert failed: {result.stderr}"
        payload = json.loads(result.stdout)
        return payload["task_id"]

    def test_task_insert_scope_flags(self, db_path):
        """--scope, --creates, --unbounded each insert task_scope rows with
        the matching source attribution (criterion 2198)."""
        # --scope and --creates can repeat; --unbounded is a single flag.
        scope_id = self._insert_with_scope_flags(
            str(db_path),
            [
                "--scope", "bin/declared_a.py",
                "--scope", "bin/declared_b.py",
                "--creates", "bin/new_file.py",
                "--creates", "tests/integration/test_new.py",
            ],
        )

        rows = _scope_rows(str(db_path), scope_id)
        by_source = {}
        for r in rows:
            by_source.setdefault(r["source"], set()).add(r["pattern"])

        assert by_source.get("operator_declared") == {
            "bin/declared_a.py", "bin/declared_b.py",
        }, by_source
        assert by_source.get("creates") == {
            "bin/new_file.py", "tests/integration/test_new.py",
        }, by_source

    def test_task_insert_unbounded_flag(self, db_path):
        """--unbounded inserts a single source='unbounded' row."""
        unbounded_id = self._insert_with_scope_flags(
            str(db_path),
            ["--unbounded"],
        )

        rows = _scope_rows(str(db_path), unbounded_id)
        unbounded_rows = [r for r in rows if r["source"] == "unbounded"]
        assert len(unbounded_rows) == 1, rows

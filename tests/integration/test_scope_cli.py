"""Integration tests for the ``tusk scope`` CLI surface and the
``tusk task-insert --scope/--creates/--unbounded`` flags.

Covers:
- ``scope add`` records the row with the correct implicit source and the
  reason text the operator passed (criterion 2183)
- ``scope list`` emits a JSON array of every entry for the task
- ``scope remove`` deletes one scope row by id and errors clearly for missing
  rows
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


def _scope_rows_with_ids(db: str, task_id: int) -> list:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, pattern, source, reason, locked_at, locked_by "
        "FROM task_scope WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _insert_progress(db: str, task_id: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_progress (task_id, commit_hash, commit_message) "
        "VALUES (?, 'abc1234', 'checkpoint')",
        (task_id,),
    )
    conn.commit()
    conn.close()


def _start_task(db: str, task_id: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET status = 'In Progress', started_at = datetime('now') "
        "WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    conn.close()


def _insert_committed_criterion(db: str, task_id: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(task_id, criterion, is_completed, commit_hash) "
        "VALUES (?, 'done work', 1, 'def5678')",
        (task_id,),
    )
    conn.commit()
    conn.close()


def _insert_unbounded_scope(db: str, task_id: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_scope (task_id, pattern, source, reason) "
        "VALUES (?, '**', 'unbounded', 'test unbounded')",
        (task_id,),
    )
    conn.commit()
    conn.close()


# ── scope add ────────────────────────────────────────────────────────────────

class TestScopeAdd:

    def test_scope_add_logs_reason(self, db_path):
        """`tusk scope add` records the pattern with the implicit upfront
        source and stamps the --reason text into the DB row (criterion 2183)."""
        task_id = _seed_task(str(db_path), description="add a helper to bin/")
        reason = "exploration revealed a missing helper not named in description"

        result = _run([
            "scope", "add", str(task_id),
            "bin/./tusk-scope.py",
            "--reason", reason,
        ])
        assert result.returncode == 0, result.stderr

        payload = json.loads(result.stdout)
        assert payload["task_id"] == task_id
        assert payload["pattern"] == "bin/tusk-scope.py"
        assert payload["source"] == "operator_declared"
        assert payload["reason"] == reason

        rows = _scope_rows(str(db_path), task_id)
        assert any(
            r["pattern"] == "bin/tusk-scope.py"
            and r["source"] == "operator_declared"
            and r["reason"] == reason
            for r in rows
        ), f"reason missing from DB row: {rows}"

    def test_scope_add_explicit_mid_task_source_is_preserved(self, db_path):
        task_id = _seed_task(str(db_path))

        result = _run([
            "scope", "add", str(task_id),
            "bin/tusk-scope.py",
            "--source", "expanded_mid_task",
        ])

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["source"] == "expanded_mid_task"

    def test_scope_add_defaults_to_operator_declared_before_first_checkpoint(self, db_path):
        task_id = _seed_task(str(db_path))
        _start_task(str(db_path), task_id)
        reason = "research uncovered a retention boundary"

        result = _run([
            "scope", "add", str(task_id),
            "bin/tusk-scope.py",
            "--reason", reason,
        ])

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["source"] == "operator_declared"
        assert payload["reason"] == reason

        listed = _run(["scope", "list", str(task_id)])
        assert listed.returncode == 0, listed.stderr
        assert {
            "pattern": "bin/tusk-scope.py",
            "source": "operator_declared",
            "reason": reason,
        }.items() <= json.loads(listed.stdout)[0].items()

        conn = sqlite3.connect(str(db_path))
        assert conn.execute(
            "SELECT COUNT(*) FROM task_progress WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM acceptance_criteria "
            "WHERE task_id = ? AND commit_hash IS NOT NULL",
            (task_id,),
        ).fetchone()[0] == 0
        conn.close()

    def test_scope_add_explicit_source_wins_after_checkpoint(self, db_path):
        task_id = _seed_task(str(db_path))
        _start_task(str(db_path), task_id)
        _insert_progress(str(db_path), task_id)

        result = _run([
            "scope", "add", str(task_id),
            "bin/tusk-scope.py",
            "--source", "operator_declared",
        ])

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["source"] == "operator_declared"

    def test_scope_add_defaults_to_mid_task_after_progress(self, db_path):
        task_id = _seed_task(str(db_path))
        _insert_progress(str(db_path), task_id)
        reason = "progress checkpoint exposed another path"

        result = _run([
            "scope", "add", str(task_id), "bin/tusk-scope.py",
            "--reason", reason,
        ])

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["source"] == "expanded_mid_task"
        assert payload["reason"] == reason

    def test_scope_add_defaults_to_mid_task_after_commit_hash(self, db_path):
        task_id = _seed_task(str(db_path))
        _insert_committed_criterion(str(db_path), task_id)
        reason = "committed criterion exposed another path"

        result = _run([
            "scope", "add", str(task_id), "bin/tusk-scope.py",
            "--reason", reason,
        ])

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["source"] == "expanded_mid_task"
        assert payload["reason"] == reason

    def test_scope_add_dedupes_normalized_equivalent_paths(self, db_path):
        """Equivalent spellings of the same repo-root path should not create
        duplicate scope rows that inflate later retro scope analysis."""
        task_id = _seed_task(str(db_path))

        first = _run(["scope", "add", str(task_id), "bin/./tusk-scope.py"])
        second = _run(["scope", "add", str(task_id), "./bin/tusk-scope.py"])

        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert json.loads(first.stdout)["pattern"] == "bin/tusk-scope.py"
        assert json.loads(second.stdout)["pattern"] == "bin/tusk-scope.py"
        rows = _scope_rows(str(db_path), task_id)
        assert [r["pattern"] for r in rows] == ["bin/tusk-scope.py"]

    def test_scope_add_rejects_nonexistent_path(self, db_path):
        task_id = _seed_task(str(db_path))

        result = _run([
            "scope", "add", str(task_id),
            "SplitScreenApp/SplitScreenTests.swift",
            "--reason", "base confusion",
        ])

        assert result.returncode == 2, result.stderr
        assert "does not exist" in result.stderr
        assert _scope_rows(str(db_path), task_id) == []

    def test_scope_add_noops_when_task_scope_is_unbounded(self, db_path):
        task_id = _seed_task(str(db_path))
        _insert_unbounded_scope(str(db_path), task_id)

        result = _run([
            "scope", "add", str(task_id),
            "tmp/nonexistent.md",
            "--reason", "pre-auth",
        ])

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload == {
            "task_id": task_id,
            "pattern": "tmp/nonexistent.md",
            "source": "unbounded",
            "unbounded": True,
            "note": "task scope is unbounded; no further authorization needed",
        }
        assert _scope_rows(str(db_path), task_id) == [
            {
                "pattern": "**",
                "source": "unbounded",
                "reason": "test unbounded",
                "locked_at": None,
                "locked_by": None,
            }
        ]

    def test_scope_add_unbounded_noop_ignores_explicit_creates_source(self, db_path):
        task_id = _seed_task(str(db_path))
        _insert_unbounded_scope(str(db_path), task_id)

        result = _run([
            "scope", "add", str(task_id),
            "tmp/nonexistent.md",
            "--source", "creates",
        ])

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["source"] == "unbounded"
        rows = _scope_rows(str(db_path), task_id)
        assert len(rows) == 1

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

    def test_scope_add_rejects_absolute_path(self, db_path):
        """`scope add` rejects absolute paths (issue #899) — the commit-time
        guard does literal repo-root-relative matching, so /etc/passwd-style
        patterns are noise rows that never enforce anything."""
        task_id = _seed_task(str(db_path))
        result = _run(["scope", "add", str(task_id), "/etc/passwd"])
        assert result.returncode == 2, result.stderr
        assert "repo-root-relative" in result.stderr
        assert _scope_rows(str(db_path), task_id) == []

    def test_scope_add_rejects_parent_traversal(self, db_path):
        """`scope add` rejects patterns with '..' segments (issue #899)."""
        task_id = _seed_task(str(db_path))
        result = _run(["scope", "add", str(task_id), "../escape"])
        assert result.returncode == 2, result.stderr
        assert ".." in result.stderr
        assert _scope_rows(str(db_path), task_id) == []

    def test_scope_add_rejects_embedded_parent_traversal(self, db_path):
        """`..` segments embedded mid-pattern are also rejected, not just
        leading ones."""
        task_id = _seed_task(str(db_path))
        result = _run(["scope", "add", str(task_id), "bin/../etc/passwd"])
        assert result.returncode == 2, result.stderr
        assert _scope_rows(str(db_path), task_id) == []

    def test_scope_add_accepts_normal_path(self, db_path):
        """Sanity guard: the validator must not reject legitimate
        repo-root-relative paths."""
        task_id = _seed_task(str(db_path))
        result = _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        assert result.returncode == 0, result.stderr


# ── readonly snapshot routing (issue #900) ───────────────────────────────────

class TestScopeSnapshotRouting:
    """`scope list` is read-only and must not trigger `snapshot_db`;
    mutating scope commands must trigger it."""

    @staticmethod
    def _backup_dir(db: str) -> str:
        return os.path.join(os.path.dirname(db), "backups")

    @staticmethod
    def _count_snapshots(backup_dir: str) -> int:
        if not os.path.isdir(backup_dir):
            return 0
        return sum(1 for n in os.listdir(backup_dir) if n.startswith("tasks.db."))

    def test_scope_list_does_not_snapshot(self, db_path):
        task_id = _seed_task(str(db_path))
        backup_dir = self._backup_dir(str(db_path))
        before = self._count_snapshots(backup_dir)
        result = _run(["scope", "list", str(task_id)])
        assert result.returncode == 0, result.stderr
        after = self._count_snapshots(backup_dir)
        assert after == before, (
            f"`scope list` created a snapshot ({before} -> {after}); the dispatcher "
            f"must recognise `scope list` as read-only (issue #900)"
        )

    def test_scope_add_still_snapshots(self, db_path):
        """Sanity guard: `scope add` mutates and must still snapshot."""
        task_id = _seed_task(str(db_path))
        backup_dir = self._backup_dir(str(db_path))
        before = self._count_snapshots(backup_dir)
        result = _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        assert result.returncode == 0, result.stderr
        after = self._count_snapshots(backup_dir)
        assert after > before, (
            f"`scope add` did not snapshot ({before} -> {after}); the dispatcher "
            f"narrowed readonly-routing too far"
        )

    def test_scope_lock_still_snapshots(self, db_path):
        """Sanity guard: `scope lock` mutates and must still snapshot."""
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        backup_dir = self._backup_dir(str(db_path))
        before = self._count_snapshots(backup_dir)
        result = _run(["scope", "lock", str(task_id)])
        assert result.returncode == 0, result.stderr
        after = self._count_snapshots(backup_dir)
        assert after > before, (
            f"`scope lock` did not snapshot ({before} -> {after}); the dispatcher "
            f"narrowed readonly-routing too far"
        )

    def test_scope_remove_still_snapshots(self, db_path):
        """Sanity guard: `scope remove` mutates and must snapshot too."""
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        row_id = _scope_rows_with_ids(str(db_path), task_id)[0]["id"]
        backup_dir = self._backup_dir(str(db_path))
        before = self._count_snapshots(backup_dir)
        result = _run(["scope", "remove", str(row_id)])
        assert result.returncode == 0, result.stderr
        after = self._count_snapshots(backup_dir)
        assert after > before, (
            f"`scope remove` did not snapshot ({before} -> {after}); the dispatcher "
            f"narrowed readonly-routing too far"
        )


# ── scope list ───────────────────────────────────────────────────────────────

class TestScopeList:

    def test_list_emits_json_array(self, db_path):
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        _run(["scope", "add", str(task_id), "bin/tusk-scope-paths.py"])

        result = _run(["scope", "list", str(task_id)])
        assert result.returncode == 0
        rows = json.loads(result.stdout)
        assert isinstance(rows, list)
        patterns = sorted(r["pattern"] for r in rows)
        assert patterns == ["bin/tusk-scope-paths.py", "bin/tusk-scope.py"]

    def test_list_empty_for_task_without_scope(self, db_path):
        task_id = _seed_task(str(db_path))
        result = _run(["scope", "list", str(task_id)])
        assert result.returncode == 0
        assert json.loads(result.stdout) == []


# ── scope remove ─────────────────────────────────────────────────────────────

class TestScopeRemove:

    def test_remove_deletes_one_scope_row(self, db_path):
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        _run(["scope", "add", str(task_id), "bin/tusk-scope-paths.py"])
        rows_before = _scope_rows_with_ids(str(db_path), task_id)
        remove_id = next(r["id"] for r in rows_before if r["pattern"] == "bin/tusk-scope.py")

        result = _run(["scope", "remove", str(remove_id)])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload == {
            "removed": True,
            "id": remove_id,
            "task_id": task_id,
            "pattern": "bin/tusk-scope.py",
            "source": "operator_declared",
        }

        rows_after = _scope_rows(str(db_path), task_id)
        assert [r["pattern"] for r in rows_after] == ["bin/tusk-scope-paths.py"]

    def test_remove_missing_row_errors_clearly(self, db_path):
        result = _run(["scope", "remove", "999999"])
        assert result.returncode == 1
        assert "scope row 999999 not found" in result.stderr.lower()

    def test_remove_is_documented_in_help(self, db_path):
        result = _run(["scope", "--help"])
        assert result.returncode == 0
        assert "remove" in result.stdout


# ── scope lock ───────────────────────────────────────────────────────────────

class TestScopeLock:

    def test_lock_stamps_unlocked_rows(self, db_path):
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        _run(["scope", "add", str(task_id), "bin/tusk-scope-paths.py"])

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
        _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])

        first = _run(["scope", "lock", str(task_id), "--by", "first"])
        assert json.loads(first.stdout)["rows_locked"] == 1

        second = _run(["scope", "lock", str(task_id), "--by", "second"])
        assert json.loads(second.stdout)["rows_locked"] == 0
        assert json.loads(second.stdout)["locked_by"] == "first"

        row = _scope_rows(str(db_path), task_id)[0]
        assert row["locked_by"] == "first", "already-locked rows must keep their original locked_by"


class TestImmutableScopeCheckpoint:

    def test_zero_row_lock_is_visible_and_rejects_new_addition(self, db_path):
        task_id = _seed_task(str(db_path))

        locked = _run(["scope", "lock", str(task_id), "--by", "checkpoint-owner"])
        assert locked.returncode == 0, locked.stderr
        assert json.loads(locked.stdout)["rows_locked"] == 0

        status = _run(["scope", "list", str(task_id), "--with-status"])
        assert status.returncode == 0, status.stderr
        payload = json.loads(status.stdout)
        assert payload["checkpoint"]["locked_by"] == "checkpoint-owner"
        assert payload["rows"] == []

        refused = _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        assert refused.returncode == 2
        assert "scope is locked" in refused.stderr
        assert "scope expand" in refused.stderr
        assert _scope_rows(str(db_path), task_id) == []

    def test_add_after_lock_refuses_without_mutating_rows(self, db_path):
        task_id = _seed_task(str(db_path))
        first = _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        assert first.returncode == 0, first.stderr
        _run(["scope", "lock", str(task_id), "--by", "locker"])
        before = _scope_rows(str(db_path), task_id)

        refused = _run([
            "scope", "add", str(task_id), "bin/tusk-scope-paths.py",
            "--reason", "should require audit",
        ])

        assert refused.returncode == 2
        assert "scope expand" in refused.stderr
        assert _scope_rows(str(db_path), task_id) == before

    def test_existing_locked_pattern_remains_an_idempotent_add(self, db_path):
        task_id = _seed_task(str(db_path))
        _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        _run(["scope", "lock", str(task_id), "--by", "locker"])

        duplicate = _run(["scope", "add", str(task_id), "bin/./tusk-scope.py"])

        assert duplicate.returncode == 0, duplicate.stderr
        assert json.loads(duplicate.stdout)["locked_by"] == "locker"
        assert len(_scope_rows(str(db_path), task_id)) == 1

    def test_expand_requires_checkpoint_and_records_locked_audit_row(self, db_path):
        task_id = _seed_task(str(db_path))
        no_checkpoint = _run([
            "scope", "expand", str(task_id), "bin/tusk-scope.py",
            "--reason", "not locked yet",
        ])
        assert no_checkpoint.returncode == 2
        assert "not locked" in no_checkpoint.stderr

        _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        locked = _run(["scope", "lock", str(task_id), "--by", "locker"])
        checkpoint = json.loads(locked.stdout)

        expanded = _run([
            "scope", "expand", str(task_id), "bin/tusk-scope-paths.py",
            "--reason", "exploration found the guard helper",
            "--by", "expander",
        ])

        assert expanded.returncode == 0, expanded.stderr
        row = json.loads(expanded.stdout)
        assert row["source"] == "expanded_mid_task"
        assert row["reason"] == "exploration found the guard helper"
        assert row["locked_at"] is not None
        assert row["locked_by"] == "expander"

        status = json.loads(
            _run(["scope", "list", str(task_id), "--with-status"]).stdout
        )
        assert status["checkpoint"]["locked_at"] == checkpoint["locked_at"]
        assert status["checkpoint"]["locked_by"] == "locker"

    def test_expand_creates_allows_future_path_and_requires_reason(self, db_path):
        task_id = _seed_task(str(db_path))
        _run(["scope", "lock", str(task_id), "--by", "locker"])

        missing_reason = _run([
            "scope", "expand", str(task_id), "future/new-helper.py",
            "--source", "creates",
        ])
        assert missing_reason.returncode == 2

        expanded = _run([
            "scope", "expand", str(task_id), "future/new-helper.py",
            "--source", "creates", "--reason", "new generated helper",
            "--by", "expander",
        ])
        assert expanded.returncode == 0, expanded.stderr
        payload = json.loads(expanded.stdout)
        assert payload["source"] == "creates"
        assert payload["locked_at"] is not None
        assert payload["locked_by"] == "expander"

    def test_remove_and_rederive_refuse_after_checkpoint(self, db_path):
        task_id = _seed_task(str(db_path), description="bin/tusk-scope.py")
        added = _run(["scope", "add", str(task_id), "bin/tusk-scope.py"])
        row_id = json.loads(added.stdout)["id"]
        _run(["scope", "lock", str(task_id), "--by", "locker"])
        before = _scope_rows(str(db_path), task_id)

        removed = _run(["scope", "remove", str(row_id)])
        rederived = _run(["scope", "rederive", str(task_id)])

        assert removed.returncode == 2
        assert rederived.returncode == 2
        assert "scope is locked" in removed.stderr
        assert "scope is locked" in rederived.stderr
        assert _scope_rows(str(db_path), task_id) == before

    def test_concurrent_lock_and_add_never_leave_an_unlocked_row(self, db_path):
        task_id = _seed_task(str(db_path))
        lock_proc = subprocess.Popen(
            [TUSK_BIN, "scope", "lock", str(task_id), "--by", "locker"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        add_proc = subprocess.Popen(
            [TUSK_BIN, "scope", "add", str(task_id), "bin/tusk-scope.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        lock_out, lock_err = lock_proc.communicate(timeout=30)
        add_out, add_err = add_proc.communicate(timeout=30)

        assert lock_proc.returncode == 0, (lock_out, lock_err)
        assert add_proc.returncode in {0, 2}, (add_out, add_err)
        rows = _scope_rows(str(db_path), task_id)
        assert all(row["locked_at"] is not None for row in rows)
        assert all(row["locked_by"] == "locker" for row in rows)


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

    def test_task_insert_auto_derives_dot_directory_paths(self, db_path):
        result = _run([
            "task-insert",
            "workflow env",
            "provide BUNNYCDN_CDN_HOST to .github/workflows/integration.yml",
            "--complexity", "S",
            "--criteria", "workflow file is scoped",
        ])
        assert result.returncode == 0, f"task-insert failed: {result.stderr}"

        payload = json.loads(result.stdout)
        rows = _scope_rows(str(db_path), payload["task_id"])
        assert any(
            r["pattern"] == ".github/workflows/integration.yml"
            and r["source"] == "auto_derived"
            for r in rows
        ), rows

    def test_task_insert_unbounded_flag(self, db_path):
        """--unbounded inserts a single source='unbounded' row."""
        unbounded_id = self._insert_with_scope_flags(
            str(db_path),
            ["--unbounded"],
        )

        rows = _scope_rows(str(db_path), unbounded_id)
        unbounded_rows = [r for r in rows if r["source"] == "unbounded"]
        assert len(unbounded_rows) == 1, rows

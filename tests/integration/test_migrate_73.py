"""Integration test for migrate_73 retry-after-crash gating (issue #898).

Migration 73 added ``tasks.scope_enforced`` and seeded existing rows with
``scope_enforced = 0`` so already-running tasks stay in legacy mode while
fresh inserts (after the migration) honor ``DEFAULT 1``. The original
implementation ran the override UPDATE unconditionally inside the
function. If the migration crashed after the UPDATE but before stamping
``PRAGMA user_version = 73`` (e.g. the backfill loop raised), a retry
would re-zero every ``scope_enforced`` row — including ones the operator
had set to 1 via ``tusk scope add`` between the partial run and the
retry. This regression covers the gated retry path.
"""

import importlib.util
import os
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate",
        os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


class TestMigrate73RetryGating:
    """Migration 73's UPDATE tasks SET scope_enforced = 0 must be gated on
    ``column_just_added`` so a retry-after-crash preserves operator-set
    values."""

    def test_retry_preserves_operator_set_scope_enforced(self, db_path, config_path):
        """Simulate the partial-run state: ``scope_enforced`` column already
        exists, one task has been promoted to ``scope_enforced = 1`` by an
        operator, but ``user_version`` is still 72. Re-running migrate_73
        must NOT re-zero the operator's row.
        """
        db = str(db_path)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, complexity, priority_score) "
                "VALUES ('retry guard', 'simulated partial run', 'To Do', 'Low', 'XS', 10)"
            )
            task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "UPDATE tasks SET scope_enforced = 1 WHERE id = ?", (task_id,)
            )
            conn.execute("PRAGMA user_version = 72")
            conn.commit()
        finally:
            conn.close()

        tusk_migrate.migrate_73(db, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db)
        try:
            value = conn.execute(
                "SELECT scope_enforced FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
            assert value == 1, (
                "scope_enforced was re-zeroed by the retry — column_just_added "
                "gate is missing or broken (issue #898)"
            )
            stamped = conn.execute("PRAGMA user_version").fetchone()[0]
            assert stamped == 73
        finally:
            conn.close()

    def test_idempotent_when_already_at_v73(self, db_path, config_path):
        """Short-circuit guard: when ``user_version`` is already at the
        latest migration, calling migrate_73 must be a no-op (no UPDATE,
        no rezero)."""
        db = str(db_path)
        conn = sqlite3.connect(db)
        try:
            version_before = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version_before >= 73
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, complexity, priority_score) "
                "VALUES ('idempotent', 'already migrated', 'To Do', 'Low', 'XS', 10)"
            )
            task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "UPDATE tasks SET scope_enforced = 1 WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        tusk_migrate.migrate_73(db, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db)
        try:
            value = conn.execute(
                "SELECT scope_enforced FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
            assert value == 1
        finally:
            conn.close()

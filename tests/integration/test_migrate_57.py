"""Integration test for migrate_57: add retro_findings table.

Covers:
- table creation with the exact column list required by the task
  (id, skill_run_id, task_id, category, summary, action_taken, created_at)
- FK to skill_runs is ON DELETE CASCADE (findings tied to a run vanish with
  the run)
- FK to tasks is ON DELETE SET NULL (findings outlive the task they
  reference, because cross-retro history must survive task cleanup)
- four supporting indexes are present
  (skill_run_id, task_id, category, created_at)
- schema version advances 56 → 57
- idempotent short-circuit on re-run
- FK behavior verifiable end-to-end: a seeded skill_run cascade-deletes its
  findings; a seeded task sets its findings' task_id to NULL on delete
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


@pytest.fixture()
def db_at_v56(db_path, config_path):
    """Reset the fresh-init DB back to version 56 so migrate_57 will run.

    Drops the retro_findings table if present (fresh DBs ship with it as of
    v57) so the migration's CREATE TABLE path is exercised end-to-end.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE IF EXISTS retro_findings")
    conn.execute("PRAGMA user_version = 56")
    conn.commit()
    conn.close()
    return str(db_path)


def _columns(db, table):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [r[1] for r in rows]


def _indexes(db, table):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    finally:
        conn.close()
    return sorted(r[1] for r in rows)


def _fk_list(db, table):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    finally:
        conn.close()
    # PRAGMA foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match
    return {r[2]: r[6] for r in rows}


def _seed_skill_run(db):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO skill_runs (skill_name) VALUES ('retro')"
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _seed_task(db):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, priority, complexity, priority_score)"
            " VALUES ('t', 'Medium', 'S', 0)"
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _seed_finding(db, skill_run_id, task_id, category="A", summary="s", action_taken=None):
    conn = sqlite3.connect(db)
    try:
        # Must enable FK enforcement explicitly per-connection (SQLite default is off).
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            "INSERT INTO retro_findings (skill_run_id, task_id, category, summary, action_taken)"
            " VALUES (?, ?, ?, ?, ?)",
            (skill_run_id, task_id, category, summary, action_taken),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class TestMigrate57:

    def test_creates_retro_findings_table_with_required_columns(self, db_at_v56, config_path):
        tusk_migrate.migrate_57(db_at_v56, config_path, SCRIPT_DIR)

        cols = _columns(db_at_v56, "retro_findings")
        assert cols == [
            "id",
            "skill_run_id",
            "task_id",
            "category",
            "summary",
            "action_taken",
            "created_at",
        ]

    def test_creates_all_four_indexes(self, db_at_v56, config_path):
        tusk_migrate.migrate_57(db_at_v56, config_path, SCRIPT_DIR)

        indexes = _indexes(db_at_v56, "retro_findings")
        # Auto-indexes (e.g. sqlite_autoindex_*) are filtered out when we only
        # assert subset containment via issuperset.
        assert set(indexes).issuperset({
            "idx_retro_findings_skill_run_id",
            "idx_retro_findings_task_id",
            "idx_retro_findings_category",
            "idx_retro_findings_created_at",
        })

    def test_fk_to_skill_runs_is_cascade(self, db_at_v56, config_path):
        tusk_migrate.migrate_57(db_at_v56, config_path, SCRIPT_DIR)

        fks = _fk_list(db_at_v56, "retro_findings")
        assert fks["skill_runs"] == "CASCADE"

    def test_fk_to_tasks_is_set_null(self, db_at_v56, config_path):
        tusk_migrate.migrate_57(db_at_v56, config_path, SCRIPT_DIR)

        fks = _fk_list(db_at_v56, "retro_findings")
        assert fks["tasks"] == "SET NULL"

    def test_skill_run_delete_cascades_to_findings(self, db_at_v56, config_path):
        tusk_migrate.migrate_57(db_at_v56, config_path, SCRIPT_DIR)

        run_id = _seed_skill_run(db_at_v56)
        task_id = _seed_task(db_at_v56)
        _seed_finding(db_at_v56, run_id, task_id, category="A")
        _seed_finding(db_at_v56, run_id, task_id, category="B")

        conn = sqlite3.connect(db_at_v56)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM skill_runs WHERE id = ?", (run_id,))
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM retro_findings WHERE skill_run_id = ?", (run_id,)
        ).fetchone()[0]
        conn.close()

        assert remaining == 0

    def test_task_delete_nulls_finding_task_id(self, db_at_v56, config_path):
        tusk_migrate.migrate_57(db_at_v56, config_path, SCRIPT_DIR)

        run_id = _seed_skill_run(db_at_v56)
        task_id = _seed_task(db_at_v56)
        finding_id = _seed_finding(db_at_v56, run_id, task_id, category="A")

        conn = sqlite3.connect(db_at_v56)
        conn.execute("PRAGMA foreign_keys = ON")
        # task_done refuses to delete tasks; this test uses a raw DELETE because
        # the point is FK behavior, not the public lifecycle API.
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        row_task_id = conn.execute(
            "SELECT task_id FROM retro_findings WHERE id = ?", (finding_id,)
        ).fetchone()[0]
        # The finding must survive — cross-retro history outlives the task.
        count = conn.execute(
            "SELECT COUNT(*) FROM retro_findings WHERE id = ?", (finding_id,)
        ).fetchone()[0]
        conn.close()

        assert count == 1
        assert row_task_id is None

    def test_not_null_constraints_enforced(self, db_at_v56, config_path):
        """skill_run_id, category, and summary are NOT NULL per the migration."""
        tusk_migrate.migrate_57(db_at_v56, config_path, SCRIPT_DIR)

        run_id = _seed_skill_run(db_at_v56)
        conn = sqlite3.connect(db_at_v56)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO retro_findings (skill_run_id, category) VALUES (?, 'A')",
                    (run_id,),
                )
                conn.commit()
            conn.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO retro_findings (skill_run_id, summary) VALUES (?, 's')",
                    (run_id,),
                )
                conn.commit()
            conn.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO retro_findings (category, summary) VALUES ('A', 's')"
                )
                conn.commit()
        finally:
            conn.close()

    def test_advances_schema_version_to_57(self, db_at_v56, config_path):
        assert tusk_migrate.get_version(db_at_v56) == 56
        tusk_migrate.migrate_57(db_at_v56, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v56) >= 57

    def test_idempotent_when_already_at_v57(self, db_path, config_path):
        """Fresh DB is already at v57+; re-running is a no-op short-circuit."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 57")
        conn.commit()
        conn.close()

        version_before = tusk_migrate.get_version(str(db_path))
        tusk_migrate.migrate_57(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before

"""Integration test for migrate_55: add tasks.fixes_task_id and backfill follow-up links.

Covers:
- column addition (nullable FK → tasks.id ON DELETE SET NULL)
- description-based backfill for 'fixes TASK-N', 'follow-up from TASK-N',
  'retro follow-up from TASK-N' phrasing
- self-reference is rejected (task that mentions its own id)
- dangling reference to a non-existent task is skipped
- rows already carrying fixes_task_id are not overwritten
- schema version advances 54 → 55
- idempotent short-circuit on re-run
- git log backfill silently no-ops when the DB lives outside any git repo
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
def db_at_v54(db_path, config_path):
    """Reset the fresh-init DB back to version 54 so migrate_55 will run.

    Drops the fixes_task_id column from tasks if present (fresh DBs ship with
    it as of v55) so the migration's ALTER TABLE path is exercised.
    """
    conn = sqlite3.connect(str(db_path))
    cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if "fixes_task_id" in cols:
        # SQLite 3.35+ supports ALTER TABLE DROP COLUMN; safer than rebuilding
        # the table because task_metrics / v_ready_tasks / v_chain_heads /
        # v_blocked_tasks / v_criteria_coverage all reference tasks.
        conn.execute("ALTER TABLE tasks DROP COLUMN fixes_task_id")
    conn.execute("PRAGMA user_version = 54")
    conn.commit()
    conn.close()
    return str(db_path)


def _seed_task(db, task_id, summary="t", description=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
        (task_id, summary, description),
    )
    conn.commit()
    conn.close()


def _fixes_map(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, fixes_task_id FROM tasks ORDER BY id"
    ).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


class TestMigrate55:

    def test_adds_fixes_task_id_column(self, db_at_v54, config_path):
        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v54)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        conn.close()
        assert "fixes_task_id" in cols

    def test_backfills_follow_up_from_phrasing(self, db_at_v54, config_path):
        _seed_task(db_at_v54, 10, description="original work")
        _seed_task(db_at_v54, 20, description="Follow-up from TASK-10: patch the edge case")

        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)

        assert _fixes_map(db_at_v54) == {10: None, 20: 10}

    def test_backfills_retro_follow_up_case_insensitive(self, db_at_v54, config_path):
        _seed_task(db_at_v54, 30, description="original")
        _seed_task(db_at_v54, 31, description="retro FOLLOW-UP from task-30: cleanup")

        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)

        assert _fixes_map(db_at_v54)[31] == 30

    def test_backfills_fixes_task_phrasing(self, db_at_v54, config_path):
        _seed_task(db_at_v54, 40, description="original")
        _seed_task(db_at_v54, 41, description="This fixes TASK-40 by handling None inputs")

        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)

        assert _fixes_map(db_at_v54)[41] == 40

    def test_self_reference_is_rejected(self, db_at_v54, config_path):
        _seed_task(db_at_v54, 50, description="Follow-up from TASK-50 (typo, refers to self)")

        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)

        assert _fixes_map(db_at_v54)[50] is None

    def test_dangling_reference_is_skipped(self, db_at_v54, config_path):
        _seed_task(db_at_v54, 60, description="Follow-up from TASK-999 which doesn't exist")

        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)

        assert _fixes_map(db_at_v54)[60] is None

    def test_does_not_overwrite_existing_fixes_task_id(self, db_at_v54, config_path):
        # Run the migration once to add the column.
        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)
        # Reset version so we can run the migration again.
        conn = sqlite3.connect(db_at_v54)
        conn.execute("PRAGMA user_version = 54")
        conn.commit()
        conn.close()

        _seed_task(db_at_v54, 70, description="original")
        _seed_task(db_at_v54, 71, description="original")
        # Seed task 72 with description pointing to 70, but pre-populate
        # fixes_task_id = 71. Backfill must not overwrite.
        conn = sqlite3.connect(db_at_v54)
        conn.execute(
            "INSERT INTO tasks (id, summary, description, fixes_task_id) "
            "VALUES (?, ?, ?, ?)",
            (72, "t", "Follow-up from TASK-70", 71),
        )
        conn.commit()
        conn.close()

        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)

        assert _fixes_map(db_at_v54)[72] == 71

    def test_tasks_without_matching_phrasing_stay_null(self, db_at_v54, config_path):
        _seed_task(db_at_v54, 80, description="Unrelated work mentioning TASK-79 in passing")
        _seed_task(db_at_v54, 81, description=None)

        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)

        assert _fixes_map(db_at_v54) == {80: None, 81: None}

    def test_advances_schema_version_to_55(self, db_at_v54, config_path):
        assert tusk_migrate.get_version(db_at_v54) == 54
        tusk_migrate.migrate_55(db_at_v54, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v54) >= 55

    def test_idempotent_when_already_at_v55(self, db_path, config_path):
        """Fresh DB is already at v55+ with the column present; re-running is a no-op."""
        # Stamp to 55 explicitly so the assertion is future-proof across later migrations.
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 55")
        conn.commit()
        conn.close()

        _seed_task(str(db_path), 90, description="original")
        _seed_task(str(db_path), 91, description="Follow-up from TASK-90")

        version_before = tusk_migrate.get_version(str(db_path))
        tusk_migrate.migrate_55(str(db_path), config_path, SCRIPT_DIR)

        # Short-circuits before touching any row.
        assert tusk_migrate.get_version(str(db_path)) == version_before
        assert _fixes_map(str(db_path))[91] is None

    def test_git_helper_returns_empty_outside_repo(self, tmp_path):
        """_followup_pairs_from_git walks up looking for .git; tmp_path has none."""
        import re as _re
        db = tmp_path / "lonely.db"
        db.touch()

        pairs = tusk_migrate._followup_pairs_from_git(
            str(db),
            existing_ids={1, 2},
            ref_re=_re.compile(r"follow-up from TASK-(\d+)|fixes TASK-(\d+)", _re.IGNORECASE),
            prefix_re=_re.compile(r"^\s*\[TASK-(\d+)\]"),
        )
        assert pairs == []

"""Integration test for migrate_50: split collapsed 'claude-opus-4' rows by date.

Before TASK-77, resolve_model() prefix-collapsed claude-opus-4-7 transcripts
into 'claude-opus-4' because pricing.json stopped at 4-6. Historical rows in
task_sessions.model and skill_runs.model retained that collapsed value.
migrate_50 repairs them by splitting the bucket on the 2026-04-17 cutoff:
anything on/after that is Opus 4.7, earlier rows are 4.6.
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
def db_at_v49(db_path, config_path):
    """Reset the fresh-init DB back to version 49 so migrate_50 will run."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 49")
    conn.commit()
    conn.close()
    return str(db_path)


def _seed_sessions(db, rows):
    """Insert (started_at, model) rows into task_sessions, one task per row."""
    conn = sqlite3.connect(db)
    for i, (started_at, model) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO tasks (id, summary) VALUES (?, ?)",
            (i, f"t{i}"),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, model) VALUES (?, ?, ?)",
            (i, started_at, model),
        )
    conn.commit()
    conn.close()


def _seed_skill_runs(db, rows):
    conn = sqlite3.connect(db)
    for started_at, model in rows:
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, model) VALUES (?, ?, ?)",
            ("/tusk", started_at, model),
        )
    conn.commit()
    conn.close()


def _fetch_models(db, table):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        f"SELECT started_at, model FROM {table} ORDER BY started_at"
    ).fetchall()
    conn.close()
    return rows


class TestMigrate50:

    def test_splits_task_sessions_on_cutoff(self, db_at_v49, config_path):
        _seed_sessions(db_at_v49, [
            ("2026-04-16 23:59:59", "claude-opus-4"),
            ("2026-04-17 00:00:00", "claude-opus-4"),
            ("2026-04-18 15:00:00", "claude-opus-4"),
        ])

        tusk_migrate.migrate_50(db_at_v49, config_path, SCRIPT_DIR)

        assert _fetch_models(db_at_v49, "task_sessions") == [
            ("2026-04-16 23:59:59", "claude-opus-4-6"),
            ("2026-04-17 00:00:00", "claude-opus-4-7"),
            ("2026-04-18 15:00:00", "claude-opus-4-7"),
        ]

    def test_splits_skill_runs_on_cutoff(self, db_at_v49, config_path):
        _seed_skill_runs(db_at_v49, [
            ("2026-04-10 10:00:00", "claude-opus-4"),
            ("2026-04-17 12:00:00", "claude-opus-4"),
        ])

        tusk_migrate.migrate_50(db_at_v49, config_path, SCRIPT_DIR)

        assert _fetch_models(db_at_v49, "skill_runs") == [
            ("2026-04-10 10:00:00", "claude-opus-4-6"),
            ("2026-04-17 12:00:00", "claude-opus-4-7"),
        ]

    def test_leaves_unrelated_models_untouched(self, db_at_v49, config_path):
        _seed_sessions(db_at_v49, [
            ("2026-04-18 10:00:00", "claude-sonnet-4-6"),
            ("2026-04-18 11:00:00", "claude-opus-4-5"),
            ("2026-04-18 12:00:00", None),
            ("2026-04-18 13:00:00", ""),
        ])

        tusk_migrate.migrate_50(db_at_v49, config_path, SCRIPT_DIR)

        assert _fetch_models(db_at_v49, "task_sessions") == [
            ("2026-04-18 10:00:00", "claude-sonnet-4-6"),
            ("2026-04-18 11:00:00", "claude-opus-4-5"),
            ("2026-04-18 12:00:00", None),
            ("2026-04-18 13:00:00", ""),
        ]

    def test_advances_schema_version_to_50(self, db_at_v49, config_path):
        assert tusk_migrate.get_version(db_at_v49) == 49
        tusk_migrate.migrate_50(db_at_v49, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v49) == 50

    def test_idempotent_when_already_at_v50(self, db_path, config_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 50")
        conn.commit()
        conn.close()

        # Seed a 'claude-opus-4' row AFTER bumping the version — the guard
        # should short-circuit and leave it untouched.
        _seed_sessions(str(db_path), [("2026-04-18 10:00:00", "claude-opus-4")])

        tusk_migrate.migrate_50(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == 50
        assert _fetch_models(str(db_path), "task_sessions") == [
            ("2026-04-18 10:00:00", "claude-opus-4"),
        ]

    def test_noop_on_db_with_no_collapsed_rows(self, db_at_v49, config_path):
        """Fresh or already-backfilled DBs: all UPDATEs are no-ops, version still advances."""
        _seed_sessions(db_at_v49, [("2026-04-18 10:00:00", "claude-opus-4-7")])
        _seed_skill_runs(db_at_v49, [("2026-04-10 10:00:00", "claude-opus-4-6")])

        tusk_migrate.migrate_50(db_at_v49, config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(db_at_v49) == 50
        assert _fetch_models(db_at_v49, "task_sessions") == [
            ("2026-04-18 10:00:00", "claude-opus-4-7"),
        ]
        assert _fetch_models(db_at_v49, "skill_runs") == [
            ("2026-04-10 10:00:00", "claude-opus-4-6"),
        ]

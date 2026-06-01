"""Integration test for migration 75: tasks.not_before time gating."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)


def _create_v74_shape(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP VIEW IF EXISTS task_metrics;
            DROP VIEW IF EXISTS v_ready_tasks;
            DROP VIEW IF EXISTS v_chain_heads;
            DROP VIEW IF EXISTS v_criteria_coverage;
            DROP TABLE IF EXISTS acceptance_criteria;
            DROP TABLE IF EXISTS external_blockers;
            DROP TABLE IF EXISTS task_dependencies;
            DROP TABLE IF EXISTS task_status_transitions;
            DROP TABLE IF EXISTS task_sessions;
            DROP TABLE IF EXISTS tasks;

            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'To Do',
                priority TEXT DEFAULT 'Medium',
                domain TEXT,
                assignee TEXT,
                task_type TEXT,
                priority_score INTEGER DEFAULT 0,
                expires_at TEXT,
                closed_reason TEXT,
                complexity TEXT,
                workflow TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                started_at TEXT,
                closed_at TEXT,
                merge_commit_sha TEXT,
                merge_base_sha TEXT,
                fixes_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                bakeoff_id INTEGER,
                bakeoff_shadow INTEGER NOT NULL DEFAULT 0 CHECK (bakeoff_shadow IN (0, 1)),
                scope_enforced INTEGER NOT NULL DEFAULT 1 CHECK (scope_enforced IN (0, 1))
            );
            CREATE TABLE task_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_seconds INTEGER,
                cost_dollars REAL,
                tokens_in INTEGER,
                tokens_out INTEGER,
                cache_read_tokens_in INTEGER,
                cache_write_tokens_in INTEGER,
                uncached_tokens_in INTEGER,
                lines_added INTEGER,
                lines_removed INTEGER,
                model TEXT,
                agent_name TEXT,
                peak_context_tokens INTEGER,
                first_context_tokens INTEGER,
                last_context_tokens INTEGER,
                context_window INTEGER,
                request_count INTEGER
            );
            CREATE TABLE task_status_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                changed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE task_dependencies (
                task_id INTEGER NOT NULL,
                depends_on_id INTEGER NOT NULL,
                relationship_type TEXT DEFAULT 'blocks'
            );
            CREATE TABLE external_blockers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                description TEXT,
                blocker_type TEXT,
                is_resolved INTEGER DEFAULT 0
            );
            CREATE TABLE acceptance_criteria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                criterion TEXT,
                is_completed INTEGER DEFAULT 0,
                is_deferred INTEGER DEFAULT 0
            );
            CREATE VIEW v_ready_tasks AS
            SELECT t.*
            FROM tasks t
            WHERE t.status = 'To Do'
              AND t.bakeoff_shadow = 0;
            CREATE VIEW v_chain_heads AS
            SELECT t.*
            FROM tasks t
            WHERE t.status <> 'Done'
              AND t.bakeoff_shadow = 0;
            PRAGMA user_version = 74;
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_75_adds_not_before_and_filters_ready_views(db_path, config_path):
    _create_v74_shape(db_path)

    tusk_migrate.migrate_75(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "not_before" in columns

        ready_id = conn.execute(
            "INSERT INTO tasks (summary, status, priority_score) "
            "VALUES ('ready', 'To Do', 10)"
        ).lastrowid
        future_id = conn.execute(
            "INSERT INTO tasks (summary, status, priority_score, not_before) "
            "VALUES ('future', 'To Do', 100, '2999-01-01 00:00:00')"
        ).lastrowid
        conn.commit()

        ready_ids = {r[0] for r in conn.execute("SELECT id FROM v_ready_tasks")}
        chain_ids = {r[0] for r in conn.execute("SELECT id FROM v_chain_heads")}
        assert ready_id in ready_ids
        assert future_id not in ready_ids
        assert future_id not in chain_ids

        conn.execute("SELECT not_before FROM task_metrics LIMIT 1").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert version == 75

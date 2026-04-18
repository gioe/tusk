#!/usr/bin/env python3
"""Migration runner for tusk schema upgrades.

Called by the tusk wrapper:
    tusk migrate   → tusk-migrate.py <db_path> <config_path>

Arguments:
    sys.argv[1] — absolute path to tasks.db
    sys.argv[2] — absolute path to the resolved config JSON file
"""

import json
import os
import re
import sqlite3
import subprocess
import sys


def _progress(msg: str) -> None:
    """Print a per-migration progress line only when stdout is interactive.

    Non-TTY callers (skills, CI, piped stdout) suppress the noise; the final
    summary line at the end of main() always prints regardless.
    """
    if sys.stdout.isatty():
        print(msg)


# ── Helpers ──────────────────────────────────────────────────────────────────

def db_connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def get_version(db_path: str) -> int:
    conn = db_connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return version


def set_version(db_path: str, version: int) -> None:
    conn = db_connect(db_path)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


def run_script(db_path: str, sql: str) -> None:
    """Run a multi-statement SQL script via executescript()."""
    conn = db_connect(db_path)
    conn.executescript(sql)
    conn.close()


def has_column(db_path: str, table: str, column: str) -> bool:
    conn = db_connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM pragma_table_info(?) WHERE name = ?",
        (table, column),
    ).fetchone()[0]
    conn.close()
    return count > 0


def has_table(db_path: str, table: str) -> bool:
    conn = db_connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()[0]
    conn.close()
    return count > 0


def generate_triggers(config_path: str, script_dir: str) -> str:
    result = subprocess.run(
        ["python3", os.path.join(script_dir, "tusk-config-tools.py"), "gen-triggers", config_path],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def drop_validate_triggers(db_path: str) -> str:
    """Return SQL statements to drop all validate_* triggers."""
    conn = db_connect(db_path)
    rows = conn.execute(
        "SELECT 'DROP TRIGGER IF EXISTS ' || name || ';' "
        "FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'validate_%';"
    ).fetchall()
    conn.close()
    return "\n".join(row[0] for row in rows)


def regen_triggers(db_path: str, config_path: str, script_dir: str) -> None:
    """Drop all validate_* triggers and regenerate from config."""
    triggers_sql = generate_triggers(config_path, script_dir)
    if not triggers_sql:
        return
    drop_sql = drop_validate_triggers(db_path)
    run_script(db_path, drop_sql + "\n" + triggers_sql)


# ── Migrations ────────────────────────────────────────────────────────────────

def migrate_1(db_path: str, config_path: str, script_dir: str) -> None:
    """Add model column to task_sessions if missing."""
    if not has_column(db_path, "task_sessions", "model"):
        run_script(db_path, """
            ALTER TABLE task_sessions ADD COLUMN model TEXT;
            PRAGMA user_version = 1;
        """)
        _progress("  Migration 1: added 'model' column to task_sessions")
    else:
        set_version(db_path, 1)


def migrate_2(db_path: str, config_path: str, script_dir: str) -> None:
    """Add task_progress table if missing."""
    if not has_table(db_path, "task_progress"):
        run_script(db_path, """
            CREATE TABLE task_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                commit_hash TEXT,
                commit_message TEXT,
                files_changed TEXT,
                next_steps TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            CREATE INDEX idx_task_progress_task_id ON task_progress(task_id);
            PRAGMA user_version = 2;
        """)
        _progress("  Migration 2: created 'task_progress' table")
    else:
        set_version(db_path, 2)


def migrate_3(db_path: str, config_path: str, script_dir: str) -> None:
    """Add relationship_type column to task_dependencies."""
    if not has_column(db_path, "task_dependencies", "relationship_type"):
        run_script(db_path, """
            ALTER TABLE task_dependencies
              ADD COLUMN relationship_type TEXT DEFAULT 'blocks'
                CHECK (relationship_type IN ('blocks', 'contingent'));
            PRAGMA user_version = 3;
        """)
        _progress("  Migration 3: added 'relationship_type' column to task_dependencies")
    else:
        set_version(db_path, 3)


def migrate_4(db_path: str, config_path: str, script_dir: str) -> None:
    """Add acceptance_criteria table."""
    if not has_table(db_path, "acceptance_criteria"):
        run_script(db_path, """
            CREATE TABLE acceptance_criteria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                criterion TEXT NOT NULL,
                source TEXT DEFAULT 'original',
                is_completed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                CHECK (source IN ('original', 'subsumption', 'pr_review')),
                CHECK (is_completed IN (0, 1))
            );
            CREATE INDEX idx_acceptance_criteria_task_id ON acceptance_criteria(task_id);
            PRAGMA user_version = 4;
        """)
        _progress("  Migration 4: created 'acceptance_criteria' table")
    else:
        set_version(db_path, 4)


def migrate_5(db_path: str, config_path: str, script_dir: str) -> None:
    """Add complexity column to tasks; recreate task_metrics view; regen triggers."""
    if not has_column(db_path, "tasks", "complexity"):
        run_script(db_path, """
            ALTER TABLE tasks ADD COLUMN complexity TEXT;
            DROP VIEW IF EXISTS task_metrics;
            CREATE VIEW task_metrics AS
            SELECT t.*,
                COUNT(s.id) as session_count,
                SUM(s.duration_seconds) as total_duration_seconds,
                SUM(s.cost_dollars) as total_cost,
                SUM(s.tokens_in) as total_tokens_in,
                SUM(s.tokens_out) as total_tokens_out,
                SUM(s.lines_added) as total_lines_added,
                SUM(s.lines_removed) as total_lines_removed
            FROM tasks t
            LEFT JOIN task_sessions s ON t.id = s.task_id
            GROUP BY t.id;
            PRAGMA user_version = 5;
        """)
        regen_triggers(db_path, config_path, script_dir)
        _progress("  Migration 5: added 'complexity' column to tasks")
    else:
        set_version(db_path, 5)


def migrate_6(db_path: str, config_path: str, script_dir: str) -> None:
    """Add external_blockers table; regen triggers."""
    if not has_table(db_path, "external_blockers"):
        run_script(db_path, """
            CREATE TABLE external_blockers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                blocker_type TEXT,
                is_resolved INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                resolved_at TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                CHECK (is_resolved IN (0, 1))
            );
            CREATE INDEX idx_external_blockers_task_id ON external_blockers(task_id);
            PRAGMA user_version = 6;
        """)
        regen_triggers(db_path, config_path, script_dir)
        _progress("  Migration 6: created 'external_blockers' table")
    else:
        set_version(db_path, 6)


def migrate_7(db_path: str, config_path: str, script_dir: str) -> None:
    """Add cost tracking columns to acceptance_criteria."""
    if not has_column(db_path, "acceptance_criteria", "completed_at"):
        run_script(db_path, """
            ALTER TABLE acceptance_criteria ADD COLUMN completed_at TEXT;
            ALTER TABLE acceptance_criteria ADD COLUMN cost_dollars REAL;
            ALTER TABLE acceptance_criteria ADD COLUMN tokens_in INTEGER;
            ALTER TABLE acceptance_criteria ADD COLUMN tokens_out INTEGER;
            PRAGMA user_version = 7;
        """)
        _progress("  Migration 7: added cost tracking columns to acceptance_criteria")
    else:
        set_version(db_path, 7)


def migrate_8(db_path: str, config_path: str, script_dir: str) -> None:
    """Add typed criteria columns to acceptance_criteria; regen triggers."""
    if not has_column(db_path, "acceptance_criteria", "criterion_type"):
        run_script(db_path, """
            ALTER TABLE acceptance_criteria ADD COLUMN criterion_type TEXT DEFAULT 'manual';
            ALTER TABLE acceptance_criteria ADD COLUMN verification_spec TEXT;
            ALTER TABLE acceptance_criteria ADD COLUMN verification_result TEXT;
            PRAGMA user_version = 8;
        """)
        regen_triggers(db_path, config_path, script_dir)
        _progress("  Migration 8: added typed criteria columns to acceptance_criteria")
    else:
        set_version(db_path, 8)


def migrate_9(db_path: str, config_path: str, script_dir: str) -> None:
    """Add commit_hash column to acceptance_criteria."""
    if not has_column(db_path, "acceptance_criteria", "commit_hash"):
        run_script(db_path, """
            ALTER TABLE acceptance_criteria ADD COLUMN commit_hash TEXT;
            PRAGMA user_version = 9;
        """)
        _progress("  Migration 9: added commit_hash column to acceptance_criteria")
    else:
        set_version(db_path, 9)


def migrate_10(db_path: str, config_path: str, script_dir: str) -> None:
    """Add committed_at column to acceptance_criteria."""
    if not has_column(db_path, "acceptance_criteria", "committed_at"):
        run_script(db_path, """
            ALTER TABLE acceptance_criteria ADD COLUMN committed_at TEXT;
            PRAGMA user_version = 10;
        """)
        _progress("  Migration 10: added committed_at column to acceptance_criteria")
    else:
        set_version(db_path, 10)


def migrate_11(db_path: str, config_path: str, script_dir: str) -> None:
    """Add code_reviews and review_comments tables; regen triggers."""
    if not has_table(db_path, "code_reviews"):
        run_script(db_path, """
            CREATE TABLE code_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                reviewer TEXT,
                status TEXT DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'approved', 'changes_requested')),
                review_pass INTEGER DEFAULT 1,
                diff_summary TEXT,
                cost_dollars REAL,
                tokens_in INTEGER,
                tokens_out INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            CREATE INDEX idx_code_reviews_task_id ON code_reviews(task_id);

            CREATE TABLE review_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                file_path TEXT,
                line_start INTEGER,
                line_end INTEGER,
                category TEXT,
                severity TEXT,
                comment TEXT NOT NULL,
                resolution TEXT DEFAULT 'pending'
                    CHECK (resolution IN ('pending', 'fixed', 'deferred', 'dismissed')),
                deferred_task_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (review_id) REFERENCES code_reviews(id) ON DELETE CASCADE,
                FOREIGN KEY (deferred_task_id) REFERENCES tasks(id)
            );
            CREATE INDEX idx_review_comments_review_id ON review_comments(review_id);

            PRAGMA user_version = 11;
        """)
        regen_triggers(db_path, config_path, script_dir)
        _progress("  Migration 11: created 'code_reviews' and 'review_comments' tables")
    else:
        set_version(db_path, 11)


def migrate_12(db_path: str, config_path: str, script_dir: str) -> None:
    """Add is_deferred and deferred_reason columns to acceptance_criteria."""
    if not has_column(db_path, "acceptance_criteria", "is_deferred"):
        run_script(db_path, """
            ALTER TABLE acceptance_criteria ADD COLUMN is_deferred INTEGER DEFAULT 0;
            ALTER TABLE acceptance_criteria ADD COLUMN deferred_reason TEXT;
            PRAGMA user_version = 12;
        """)
        _progress("  Migration 12: added is_deferred and deferred_reason columns to acceptance_criteria")
    else:
        set_version(db_path, 12)


def migrate_13(db_path: str, config_path: str, script_dir: str) -> None:
    """Add status transition validation trigger."""
    triggers_sql = generate_triggers(config_path, script_dir)
    if triggers_sql:
        drop_sql = drop_validate_triggers(db_path)
        run_script(db_path, drop_sql + "\n" + triggers_sql + "\nPRAGMA user_version = 13;")
    else:
        set_version(db_path, 13)
    _progress("  Migration 13: added status transition validation trigger")


def migrate_14(db_path: str, config_path: str, script_dir: str) -> None:
    """Create v_ready_tasks view."""
    run_script(db_path, """
        CREATE VIEW IF NOT EXISTS v_ready_tasks AS
        SELECT t.*
        FROM tasks t
        WHERE t.status = 'To Do'
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );
        PRAGMA user_version = 14;
    """)
    _progress("  Migration 14: created v_ready_tasks view")


def migrate_15(db_path: str, config_path: str, script_dir: str) -> None:
    """Add v_chain_heads, v_blocked_tasks, v_criteria_coverage views."""
    run_script(db_path, """
        CREATE VIEW IF NOT EXISTS v_chain_heads AS
        SELECT t.*
        FROM tasks t
        WHERE t.status <> 'Done'
          AND EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks downstream ON d.task_id = downstream.id
            WHERE d.depends_on_id = t.id AND downstream.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );

        CREATE VIEW IF NOT EXISTS v_blocked_tasks AS
        SELECT t.id, t.summary, t.status, t.priority, t.domain, t.assignee,
               'dependency' AS block_reason,
               blocker.id AS blocking_id,
               blocker.summary AS blocking_summary
        FROM tasks t
        JOIN task_dependencies d ON d.task_id = t.id
        JOIN tasks blocker ON d.depends_on_id = blocker.id
        WHERE t.status <> 'Done' AND blocker.status <> 'Done'
        UNION ALL
        SELECT t.id, t.summary, t.status, t.priority, t.domain, t.assignee,
               'external_blocker' AS block_reason,
               eb.id AS blocking_id,
               eb.description AS blocking_summary
        FROM tasks t
        JOIN external_blockers eb ON eb.task_id = t.id
        WHERE t.status <> 'Done' AND eb.is_resolved = 0;

        CREATE VIEW IF NOT EXISTS v_criteria_coverage AS
        SELECT t.id AS task_id,
               t.summary,
               COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) AS total_criteria,
               COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS completed_criteria,
               COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) - COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS remaining_criteria
        FROM tasks t
        LEFT JOIN acceptance_criteria ac ON ac.task_id = t.id
        GROUP BY t.id, t.summary;

        PRAGMA user_version = 15;
    """)
    _progress("  Migration 15: added v_chain_heads, v_blocked_tasks, v_criteria_coverage views")


def migrate_16(db_path: str, config_path: str, script_dir: str) -> None:
    """Fix v_ready_tasks to exclude contingent deps from readiness check."""
    run_script(db_path, """
        DROP VIEW IF EXISTS v_ready_tasks;

        CREATE VIEW v_ready_tasks AS
        SELECT t.*
        FROM tasks t
        WHERE t.status = 'To Do'
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );

        PRAGMA user_version = 16;
    """)
    _progress("  Migration 16: fixed v_ready_tasks to only filter 'blocks'-type deps (not contingent)")


def migrate_17(db_path: str, config_path: str, script_dir: str) -> None:
    """Fix v_chain_heads and v_blocked_tasks to exclude contingent deps."""
    run_script(db_path, """
        DROP VIEW IF EXISTS v_chain_heads;
        DROP VIEW IF EXISTS v_blocked_tasks;

        CREATE VIEW v_chain_heads AS
        SELECT t.*
        FROM tasks t
        WHERE t.status <> 'Done'
          AND EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks downstream ON d.task_id = downstream.id
            WHERE d.depends_on_id = t.id AND downstream.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );

        CREATE VIEW v_blocked_tasks AS
        SELECT t.id, t.summary, t.status, t.priority, t.domain, t.assignee,
               'dependency' AS block_reason,
               blocker.id AS blocking_id,
               blocker.summary AS blocking_summary
        FROM tasks t
        JOIN task_dependencies d ON d.task_id = t.id
        JOIN tasks blocker ON d.depends_on_id = blocker.id
        WHERE t.status <> 'Done' AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
        UNION ALL
        SELECT t.id, t.summary, t.status, t.priority, t.domain, t.assignee,
               'external_blocker' AS block_reason,
               eb.id AS blocking_id,
               eb.description AS blocking_summary
        FROM tasks t
        JOIN external_blockers eb ON eb.task_id = t.id
        WHERE t.status <> 'Done' AND eb.is_resolved = 0;

        PRAGMA user_version = 17;
    """)
    _progress("  Migration 17: fixed v_chain_heads and v_blocked_tasks to only filter 'blocks'-type deps (not contingent)")


def migrate_18(db_path: str, config_path: str, script_dir: str) -> None:
    """Add skill_runs table for per-skill cost tracking."""
    run_script(db_path, """
        CREATE TABLE IF NOT EXISTS skill_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            cost_dollars REAL,
            tokens_in INTEGER,
            tokens_out INTEGER,
            model TEXT,
            metadata TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_skill_runs_skill_name ON skill_runs(skill_name);
        PRAGMA user_version = 18;
    """)
    _progress("  Migration 18: added skill_runs table for per-skill cost tracking")


def migrate_19(db_path: str, config_path: str, script_dir: str) -> None:
    """Add tool_call_stats table for pre-computed per-tool-call cost aggregates."""
    run_script(db_path, """
        CREATE TABLE IF NOT EXISTS tool_call_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            task_id INTEGER,
            tool_name TEXT NOT NULL,
            call_count INTEGER NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0.0,
            max_cost REAL NOT NULL DEFAULT 0.0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES task_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
            UNIQUE (session_id, tool_name)
        );
        CREATE INDEX IF NOT EXISTS idx_tool_call_stats_session_id ON tool_call_stats(session_id);
        CREATE INDEX IF NOT EXISTS idx_tool_call_stats_task_id ON tool_call_stats(task_id);
        PRAGMA user_version = 19;
    """)
    _progress("  Migration 19: added tool_call_stats table for per-tool-call cost aggregates")


def migrate_20(db_path: str, config_path: str, script_dir: str) -> None:
    """Add skill_run_id FK to tool_call_stats; make session_id nullable."""
    run_script(db_path, """
        BEGIN;

        CREATE TABLE tool_call_stats_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            task_id INTEGER,
            skill_run_id INTEGER,
            tool_name TEXT NOT NULL,
            call_count INTEGER NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0.0,
            max_cost REAL NOT NULL DEFAULT 0.0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES task_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
            FOREIGN KEY (skill_run_id) REFERENCES skill_runs(id) ON DELETE CASCADE,
            UNIQUE (session_id, tool_name),
            UNIQUE (skill_run_id, tool_name)
        );

        INSERT INTO tool_call_stats_new (id, session_id, task_id, tool_name, call_count, total_cost, max_cost, tokens_out, computed_at)
        SELECT id, session_id, task_id, tool_name, call_count, total_cost, max_cost, tokens_out, computed_at
        FROM tool_call_stats;

        DROP TABLE tool_call_stats;
        ALTER TABLE tool_call_stats_new RENAME TO tool_call_stats;

        CREATE INDEX idx_tool_call_stats_session_id ON tool_call_stats(session_id);
        CREATE INDEX idx_tool_call_stats_task_id ON tool_call_stats(task_id);
        CREATE INDEX idx_tool_call_stats_skill_run_id ON tool_call_stats(skill_run_id);

        PRAGMA user_version = 20;

        COMMIT;
    """)
    _progress("  Migration 20: added skill_run_id FK to tool_call_stats, made session_id nullable")


def migrate_21(db_path: str, config_path: str, script_dir: str) -> None:
    """Add CHECK constraint to tool_call_stats (session_id or skill_run_id must be set)."""
    run_script(db_path, """
        BEGIN;

        CREATE TABLE tool_call_stats_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            task_id INTEGER,
            skill_run_id INTEGER,
            tool_name TEXT NOT NULL,
            call_count INTEGER NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0.0,
            max_cost REAL NOT NULL DEFAULT 0.0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES task_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
            FOREIGN KEY (skill_run_id) REFERENCES skill_runs(id) ON DELETE CASCADE,
            UNIQUE (session_id, tool_name),
            UNIQUE (skill_run_id, tool_name),
            CHECK (session_id IS NOT NULL OR skill_run_id IS NOT NULL)
        );

        INSERT INTO tool_call_stats_new (id, session_id, task_id, skill_run_id, tool_name, call_count, total_cost, max_cost, tokens_out, computed_at)
        SELECT id, session_id, task_id, skill_run_id, tool_name, call_count, total_cost, max_cost, tokens_out, computed_at
        FROM tool_call_stats;

        DROP TABLE tool_call_stats;
        ALTER TABLE tool_call_stats_new RENAME TO tool_call_stats;

        CREATE INDEX idx_tool_call_stats_session_id ON tool_call_stats(session_id);
        CREATE INDEX idx_tool_call_stats_task_id ON tool_call_stats(task_id);
        CREATE INDEX idx_tool_call_stats_skill_run_id ON tool_call_stats(skill_run_id);

        PRAGMA user_version = 21;

        COMMIT;
    """)
    _progress("  Migration 21: added CHECK(session_id IS NOT NULL OR skill_run_id IS NOT NULL) to tool_call_stats")


def migrate_22(db_path: str, config_path: str, script_dir: str) -> None:
    """Add criterion_id FK to tool_call_stats for per-criterion tool-cost drilldown."""
    run_script(db_path, """
        BEGIN;

        CREATE TABLE tool_call_stats_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            task_id INTEGER,
            skill_run_id INTEGER,
            criterion_id INTEGER,
            tool_name TEXT NOT NULL,
            call_count INTEGER NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0.0,
            max_cost REAL NOT NULL DEFAULT 0.0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES task_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
            FOREIGN KEY (skill_run_id) REFERENCES skill_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (criterion_id) REFERENCES acceptance_criteria(id) ON DELETE CASCADE,
            UNIQUE (session_id, tool_name),
            UNIQUE (skill_run_id, tool_name),
            UNIQUE (criterion_id, tool_name),
            CHECK (session_id IS NOT NULL OR skill_run_id IS NOT NULL OR criterion_id IS NOT NULL)
        );

        INSERT INTO tool_call_stats_new (id, session_id, task_id, skill_run_id, tool_name, call_count, total_cost, max_cost, tokens_out, computed_at)
        SELECT id, session_id, task_id, skill_run_id, tool_name, call_count, total_cost, max_cost, tokens_out, computed_at
        FROM tool_call_stats;

        DROP TABLE tool_call_stats;
        ALTER TABLE tool_call_stats_new RENAME TO tool_call_stats;

        CREATE INDEX idx_tool_call_stats_session_id ON tool_call_stats(session_id);
        CREATE INDEX idx_tool_call_stats_task_id ON tool_call_stats(task_id);
        CREATE INDEX idx_tool_call_stats_skill_run_id ON tool_call_stats(skill_run_id);
        CREATE INDEX idx_tool_call_stats_criterion_id ON tool_call_stats(criterion_id);

        PRAGMA user_version = 22;

        COMMIT;
    """)
    _progress("  Migration 22: added criterion_id FK to tool_call_stats for per-criterion tool-cost drilldown")


def migrate_23(db_path: str, config_path: str, script_dir: str) -> None:
    """Add tokens_in column to tool_call_stats for per-tool input-token tracking."""
    run_script(db_path, """
        ALTER TABLE tool_call_stats ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0;
        PRAGMA user_version = 23;
    """)
    _progress("  Migration 23: added tokens_in column to tool_call_stats")


def migrate_24(db_path: str, config_path: str, script_dir: str) -> None:
    """Add v_velocity view for task throughput and cost-per-task metrics by calendar week."""
    run_script(db_path, """
        CREATE VIEW IF NOT EXISTS v_velocity AS
        SELECT
            strftime('%Y-W%W', updated_at) AS week,
            COUNT(id) AS task_count,
            AVG(total_cost) AS avg_cost,
            AVG(total_tokens_in) AS avg_tokens_in,
            AVG(total_tokens_out) AS avg_tokens_out
        FROM task_metrics
        WHERE status = 'Done' AND closed_reason = 'completed'
        GROUP BY strftime('%Y-W%W', updated_at);
        PRAGMA user_version = 24;
    """)
    _progress("  Migration 24: added v_velocity view for throughput and cost-per-task metrics")


def migrate_25(db_path: str, config_path: str, script_dir: str) -> None:
    """Drop github_pr column from tasks table."""
    drop_triggers = drop_validate_triggers(db_path)

    run_script(db_path, f"""
        BEGIN;

        {drop_triggers}

        DROP VIEW IF EXISTS v_velocity;
        DROP VIEW IF EXISTS v_criteria_coverage;
        DROP VIEW IF EXISTS v_blocked_tasks;
        DROP VIEW IF EXISTS v_chain_heads;
        DROP VIEW IF EXISTS v_ready_tasks;
        DROP VIEW IF EXISTS task_metrics;

        CREATE TABLE tasks_new (
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
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        INSERT INTO tasks_new (id, summary, description, status, priority, domain, assignee, task_type, priority_score, expires_at, closed_reason, complexity, created_at, updated_at)
        SELECT id, summary, description, status, priority, domain, assignee, task_type, priority_score, expires_at, closed_reason, complexity, created_at, updated_at
        FROM tasks;

        DROP TABLE tasks;
        ALTER TABLE tasks_new RENAME TO tasks;

        CREATE VIEW task_metrics AS
        SELECT t.*,
            COUNT(s.id) as session_count,
            SUM(s.duration_seconds) as total_duration_seconds,
            SUM(s.cost_dollars) as total_cost,
            SUM(s.tokens_in) as total_tokens_in,
            SUM(s.tokens_out) as total_tokens_out,
            SUM(s.lines_added) as total_lines_added,
            SUM(s.lines_removed) as total_lines_removed
        FROM tasks t
        LEFT JOIN task_sessions s ON t.id = s.task_id
        GROUP BY t.id;

        CREATE VIEW v_ready_tasks AS
        SELECT t.*
        FROM tasks t
        WHERE t.status = 'To Do'
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );

        CREATE VIEW v_chain_heads AS
        SELECT t.*
        FROM tasks t
        WHERE t.status <> 'Done'
          AND EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks downstream ON d.task_id = downstream.id
            WHERE d.depends_on_id = t.id AND downstream.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );

        CREATE VIEW v_blocked_tasks AS
        SELECT t.id, t.summary, t.status, t.priority, t.domain, t.assignee,
               'dependency' AS block_reason,
               blocker.id AS blocking_id,
               blocker.summary AS blocking_summary
        FROM tasks t
        JOIN task_dependencies d ON d.task_id = t.id
        JOIN tasks blocker ON d.depends_on_id = blocker.id
        WHERE t.status <> 'Done' AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
        UNION ALL
        SELECT t.id, t.summary, t.status, t.priority, t.domain, t.assignee,
               'external_blocker' AS block_reason,
               eb.id AS blocking_id,
               eb.description AS blocking_summary
        FROM tasks t
        JOIN external_blockers eb ON eb.task_id = t.id
        WHERE t.status <> 'Done' AND eb.is_resolved = 0;

        CREATE VIEW v_criteria_coverage AS
        SELECT t.id AS task_id,
               t.summary,
               COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) AS total_criteria,
               COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS completed_criteria,
               COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) - COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS remaining_criteria
        FROM tasks t
        LEFT JOIN acceptance_criteria ac ON ac.task_id = t.id
        GROUP BY t.id, t.summary;

        CREATE VIEW v_velocity AS
        SELECT
            strftime('%Y-W%W', updated_at) AS week,
            COUNT(id) AS task_count,
            AVG(total_cost) AS avg_cost,
            AVG(total_tokens_in) AS avg_tokens_in,
            AVG(total_tokens_out) AS avg_tokens_out
        FROM task_metrics
        WHERE status = 'Done' AND closed_reason = 'completed'
        GROUP BY strftime('%Y-W%W', updated_at);

        PRAGMA user_version = 25;

        COMMIT;
    """)

    regen_triggers(db_path, config_path, script_dir)
    _progress("  Migration 25: dropped github_pr column from tasks table")


def migrate_26(db_path: str, config_path: str, script_dir: str) -> None:
    """Add agent_name column to task_sessions and code_reviews."""
    run_script(db_path, """
        ALTER TABLE task_sessions ADD COLUMN agent_name TEXT;
        ALTER TABLE code_reviews ADD COLUMN agent_name TEXT;
        PRAGMA user_version = 26;
    """)
    _progress("  Migration 26: added agent_name column to task_sessions and code_reviews")


def migrate_27(db_path: str, config_path: str, script_dir: str) -> None:
    """Add partial UNIQUE index on task_sessions(task_id) WHERE ended_at IS NULL."""
    run_script(db_path, """
        DELETE FROM task_sessions
        WHERE ended_at IS NULL
          AND id NOT IN (
            SELECT MAX(id) FROM task_sessions WHERE ended_at IS NULL GROUP BY task_id
          );

        CREATE UNIQUE INDEX idx_task_sessions_open ON task_sessions(task_id) WHERE ended_at IS NULL;

        PRAGMA user_version = 27;
    """)
    _progress("  Migration 27: added partial UNIQUE index on task_sessions(task_id) WHERE ended_at IS NULL")


def migrate_28(db_path: str, config_path: str, script_dir: str) -> None:
    """Add is_deferred boolean column to tasks and backfill from [Deferred] prefix."""
    run_script(db_path, """
        ALTER TABLE tasks ADD COLUMN is_deferred INTEGER NOT NULL DEFAULT 0 CHECK (is_deferred IN (0, 1));
        UPDATE tasks SET is_deferred = 1 WHERE summary LIKE '[Deferred]%';
        PRAGMA user_version = 28;
    """)
    _progress("  Migration 28: added is_deferred column to tasks and backfilled from [Deferred] prefix")


def migrate_29(db_path: str, config_path: str, script_dir: str) -> None:
    """Add conventions table and import existing conventions.md."""
    run_script(db_path, """
        CREATE TABLE conventions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            source_skill TEXT,
            lint_rule TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        PRAGMA user_version = 29;
    """)

    # Import existing conventions.md (idempotent: skip if already imported)
    repo_root = os.path.dirname(os.path.dirname(db_path))
    conventions_md = os.path.join(repo_root, "tusk", "conventions.md")
    if os.path.isfile(conventions_md):
        conn = db_connect(db_path)
        existing = conn.execute("SELECT COUNT(*) FROM conventions").fetchone()[0]
        if existing > 0:
            conn.close()
            _progress(f"  Skipped import: {existing} convention(s) already in DB")
        else:
            try:
                with open(conventions_md) as f:
                    content = f.read()
                blocks = re.split(r'(?m)^(?=## )', content)
                count = 0
                for block in blocks:
                    block = block.strip()
                    if not block.startswith('## '):
                        continue
                    date_match = re.search(r'_Source: session \d+ — (\d{4}-\d{2}-\d{2})_', block)
                    created_at = date_match.group(1) if date_match else None
                    text = re.sub(r'\n_Source: session \d+ — \d{4}-\d{2}-\d{2}_', '', block).strip()
                    conn.execute(
                        "INSERT INTO conventions (text, source_skill, created_at) VALUES (?, ?, ?)",
                        (text, 'retro', created_at),
                    )
                    count += 1
                conn.commit()
                _progress(f"  Imported {count} convention(s) from conventions.md")
            except Exception as e:
                conn.rollback()
                print(f"  Warning: conventions.md import failed: {e}", file=sys.stderr)
                print("  Re-run 'tusk migrate' to retry the import (idempotent when table is empty).", file=sys.stderr)
            finally:
                conn.close()

    _progress("  Migration 29: added conventions table")


def migrate_30(db_path: str, config_path: str, script_dir: str) -> None:
    """Drop pending from review_comments.resolution; NULL is now the unresolved sentinel."""
    drop_triggers = drop_validate_triggers(db_path)

    run_script(db_path, f"""
        BEGIN;

        {drop_triggers}

        CREATE TABLE review_comments_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            file_path TEXT,
            line_start INTEGER,
            line_end INTEGER,
            category TEXT,
            severity TEXT,
            comment TEXT NOT NULL,
            resolution TEXT DEFAULT NULL
                CHECK (resolution IN ('fixed', 'deferred', 'dismissed')),
            deferred_task_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (review_id) REFERENCES code_reviews(id) ON DELETE CASCADE,
            FOREIGN KEY (deferred_task_id) REFERENCES tasks(id)
        );

        INSERT INTO review_comments_new (id, review_id, file_path, line_start, line_end, category, severity, comment, resolution, deferred_task_id, created_at, updated_at)
        SELECT id, review_id, file_path, line_start, line_end, category, severity, comment,
            CASE WHEN resolution = 'pending' THEN NULL ELSE resolution END,
            deferred_task_id, created_at, updated_at
        FROM review_comments;

        DROP TABLE review_comments;
        ALTER TABLE review_comments_new RENAME TO review_comments;

        CREATE INDEX idx_review_comments_review_id ON review_comments(review_id);

        PRAGMA user_version = 30;

        COMMIT;
    """)

    regen_triggers(db_path, config_path, script_dir)
    _progress("  Migration 30: dropped pending from review_comments.resolution; NULL is now the unresolved sentinel")


def migrate_31(db_path: str, config_path: str, script_dir: str) -> None:
    """Add lint_rules table."""
    if not has_table(db_path, "lint_rules"):
        run_script(db_path, """
            CREATE TABLE lint_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grep_pattern TEXT NOT NULL,
                file_glob TEXT NOT NULL,
                message TEXT NOT NULL,
                is_blocking INTEGER NOT NULL DEFAULT 0,
                source_skill TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                CHECK (is_blocking IN (0, 1))
            );
            PRAGMA user_version = 31;
        """)
    else:
        set_version(db_path, 31)
    _progress("  Migration 31: added lint_rules table")


def migrate_32(db_path: str, config_path: str, script_dir: str) -> None:
    """Add tool_call_events table."""
    if not has_table(db_path, "tool_call_events"):
        run_script(db_path, """
            CREATE TABLE tool_call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                session_id INTEGER,
                criterion_id INTEGER,
                tool_name TEXT NOT NULL,
                cost_dollars REAL NOT NULL DEFAULT 0.0,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                call_sequence INTEGER NOT NULL DEFAULT 0,
                called_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
                FOREIGN KEY (session_id) REFERENCES task_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (criterion_id) REFERENCES acceptance_criteria(id) ON DELETE CASCADE,
                CHECK (session_id IS NOT NULL OR criterion_id IS NOT NULL)
            );
            CREATE INDEX idx_tool_call_events_session_id ON tool_call_events(session_id);
            CREATE INDEX idx_tool_call_events_task_id ON tool_call_events(task_id);
            CREATE INDEX idx_tool_call_events_criterion_id ON tool_call_events(criterion_id);
            PRAGMA user_version = 32;
        """)
    else:
        set_version(db_path, 32)
    _progress("  Migration 32: added tool_call_events table")


def migrate_33(db_path: str, config_path: str, script_dir: str) -> None:
    """Add qualitative boolean column to conventions table."""
    if not has_column(db_path, "conventions", "qualitative"):
        run_script(db_path, """
            ALTER TABLE conventions ADD COLUMN qualitative INTEGER NOT NULL DEFAULT 0;
        """)
    set_version(db_path, 33)
    _progress("  Migration 33: added qualitative column to conventions")


def migrate_34(db_path: str, config_path: str, script_dir: str) -> None:
    """Add skill_run_id FK to tool_call_events and update CHECK constraint."""
    if not has_column(db_path, "tool_call_events", "skill_run_id"):
        run_script(db_path, """
            BEGIN;

            CREATE TABLE tool_call_events_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                session_id INTEGER,
                criterion_id INTEGER,
                skill_run_id INTEGER,
                tool_name TEXT NOT NULL,
                cost_dollars REAL NOT NULL DEFAULT 0.0,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                call_sequence INTEGER NOT NULL DEFAULT 0,
                called_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
                FOREIGN KEY (session_id) REFERENCES task_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (criterion_id) REFERENCES acceptance_criteria(id) ON DELETE CASCADE,
                FOREIGN KEY (skill_run_id) REFERENCES skill_runs(id) ON DELETE CASCADE,
                CHECK (session_id IS NOT NULL OR criterion_id IS NOT NULL OR skill_run_id IS NOT NULL)
            );

            INSERT INTO tool_call_events_new
                (id, task_id, session_id, criterion_id, tool_name, cost_dollars, tokens_in, tokens_out, call_sequence, called_at)
            SELECT id, task_id, session_id, criterion_id, tool_name, cost_dollars, tokens_in, tokens_out, call_sequence, called_at
            FROM tool_call_events;

            DROP TABLE tool_call_events;
            ALTER TABLE tool_call_events_new RENAME TO tool_call_events;

            CREATE INDEX idx_tool_call_events_session_id ON tool_call_events(session_id);
            CREATE INDEX idx_tool_call_events_task_id ON tool_call_events(task_id);
            CREATE INDEX idx_tool_call_events_criterion_id ON tool_call_events(criterion_id);
            CREATE INDEX idx_tool_call_events_skill_run_id ON tool_call_events(skill_run_id);

            PRAGMA user_version = 34;

            COMMIT;
        """)
    else:
        set_version(db_path, 34)
    _progress("  Migration 34: added skill_run_id column to tool_call_events")


def migrate_35(db_path: str, config_path: str, script_dir: str) -> None:
    if get_version(db_path) < 35:
        run_script(db_path, """
            ALTER TABLE code_reviews ADD COLUMN note TEXT;
            PRAGMA user_version = 35;
        """)
    else:
        set_version(db_path, 35)
    _progress("  Migration 35: added note column to code_reviews")


def migrate_36(db_path: str, config_path: str, script_dir: str) -> None:
    if get_version(db_path) < 36:
        run_script(db_path, """
            ALTER TABLE tasks ADD COLUMN started_at TEXT;

            UPDATE tasks
            SET started_at = (
                SELECT MIN(s.started_at)
                FROM task_sessions s
                WHERE s.task_id = tasks.id
            )
            WHERE status IN ('In Progress', 'Done')
              AND (
                SELECT MIN(s.started_at)
                FROM task_sessions s
                WHERE s.task_id = tasks.id
              ) IS NOT NULL;

            PRAGMA user_version = 36;
        """)
    else:
        set_version(db_path, 36)
    _progress("  Migration 36: added started_at column to tasks, backfilled from task_sessions")


def migrate_37(db_path: str, config_path: str, script_dir: str) -> None:
    if get_version(db_path) < 37:
        run_script(db_path, """
            ALTER TABLE tasks ADD COLUMN closed_at TEXT;

            -- Backfill closed_at from updated_at for tasks already closed
            -- (closed_reason is preserved; this migration only adds the timestamp)
            UPDATE tasks
            SET closed_at = updated_at
            WHERE status = 'Done';

            DROP VIEW IF EXISTS v_velocity;

            CREATE VIEW v_velocity AS
            SELECT
                strftime('%Y-W%W', COALESCE(closed_at, updated_at)) AS week,
                COUNT(id) AS task_count,
                AVG(total_cost) AS avg_cost,
                AVG(total_tokens_in) AS avg_tokens_in,
                AVG(total_tokens_out) AS avg_tokens_out
            FROM task_metrics
            WHERE status = 'Done' AND closed_reason = 'completed'
            GROUP BY strftime('%Y-W%W', COALESCE(closed_at, updated_at));

            PRAGMA user_version = 37;
        """)
    else:
        set_version(db_path, 37)
    _progress("  Migration 37: added closed_at column to tasks, backfilled from updated_at for Done tasks, updated v_velocity")


def migrate_38(db_path: str, config_path: str, script_dir: str) -> None:
    if get_version(db_path) < 38:
        run_script(db_path, """
            ALTER TABLE task_sessions ADD COLUMN peak_context_tokens INTEGER;
            ALTER TABLE task_sessions ADD COLUMN first_context_tokens INTEGER;
            ALTER TABLE task_sessions ADD COLUMN last_context_tokens INTEGER;

            PRAGMA user_version = 38;
        """)
    else:
        set_version(db_path, 38)
    _progress("  Migration 38: added peak_context_tokens, first_context_tokens, last_context_tokens columns to task_sessions")


def migrate_39(db_path: str, config_path: str, script_dir: str) -> None:
    if get_version(db_path) < 39:
        run_script(db_path, """
            ALTER TABLE task_sessions ADD COLUMN context_window INTEGER;

            UPDATE task_sessions
            SET context_window = CASE
                WHEN model IN ('claude-opus-4-6', 'claude-sonnet-4-6') THEN 1000000
                ELSE 200000
            END;

            PRAGMA user_version = 39;
        """)
    else:
        set_version(db_path, 39)
    _progress("  Migration 39: added context_window column to task_sessions and back-filled from model")


def migrate_40(db_path: str, config_path: str, script_dir: str) -> None:
    """Add 'issue' to task_types in config and regenerate validation trigger."""
    if get_version(db_path) < 40:
        # Patch the installed config to include 'issue' before regenerating triggers
        with open(config_path) as f:
            cfg = json.load(f)
        if "issue" not in cfg.get("task_types", []):
            cfg.setdefault("task_types", []).append("issue")
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")
        regen_triggers(db_path, config_path, script_dir)
        set_version(db_path, 40)
    else:
        set_version(db_path, 40)
    _progress("  Migration 40: added 'issue' to task_types config and regenerated validation trigger")


def migrate_41(db_path: str, config_path: str, script_dir: str) -> None:
    """Add 'superseded' to code_reviews.status CHECK constraint via table recreation."""
    if get_version(db_path) < 41:
        run_script(db_path, """
            BEGIN;

            CREATE TABLE code_reviews_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                reviewer TEXT,
                status TEXT DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'approved', 'changes_requested', 'superseded')),
                review_pass INTEGER DEFAULT 1,
                diff_summary TEXT,
                cost_dollars REAL,
                tokens_in INTEGER,
                tokens_out INTEGER,
                agent_name TEXT,
                note TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            INSERT INTO code_reviews_new (id, task_id, reviewer, status, review_pass, diff_summary,
                cost_dollars, tokens_in, tokens_out, agent_name, note, created_at, updated_at)
            SELECT id, task_id, reviewer, status, review_pass, diff_summary,
                cost_dollars, tokens_in, tokens_out, agent_name, note, created_at, updated_at
            FROM code_reviews;

            DROP TABLE code_reviews;
            ALTER TABLE code_reviews_new RENAME TO code_reviews;

            CREATE INDEX idx_code_reviews_task_id ON code_reviews(task_id);

            PRAGMA user_version = 41;
            COMMIT;
        """)
    else:
        set_version(db_path, 41)
    _progress("  Migration 41: added 'superseded' to code_reviews.status CHECK constraint")


def migrate_42(db_path: str, config_path: str, script_dir: str) -> None:
    if not has_column(db_path, "conventions", "topics"):
        run_script(db_path, """
            BEGIN;
            ALTER TABLE conventions ADD COLUMN topics TEXT;
            PRAGMA user_version = 42;
            COMMIT;
        """)
    else:
        set_version(db_path, 42)
    _progress("  Migration 42: added 'topics' column to conventions table")


def migrate_43(db_path: str, config_path: str, script_dir: str) -> None:
    run_script(db_path, """
        BEGIN;
        UPDATE conventions
        SET topics = replace(replace(topics, ', ', ','), ' ,', ',')
        WHERE topics IS NOT NULL AND (topics LIKE '%, %' OR topics LIKE '% ,%');
        PRAGMA user_version = 43;
        COMMIT;
    """)
    _progress("  Migration 43: backfill normalize whitespace in convention topics")


def migrate_44(db_path: str, config_path: str, script_dir: str) -> None:
    if not has_table(db_path, "pillars"):
        run_script(db_path, """
            BEGIN;
            CREATE TABLE pillars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                core_claim TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            PRAGMA user_version = 44;
            COMMIT;
        """)
    else:
        set_version(db_path, 44)
    _progress("  Migration 44: added 'pillars' table")


def migrate_45(db_path: str, config_path: str, script_dir: str) -> None:
    if not has_column(db_path, "tasks", "workflow"):
        run_script(db_path, """
            BEGIN;
            ALTER TABLE tasks ADD COLUMN workflow TEXT;
            PRAGMA user_version = 45;
            COMMIT;
        """)
    else:
        set_version(db_path, 45)
    _progress("  Migration 45: added 'workflow' column to tasks table")


def migrate_46(db_path: str, config_path: str, script_dir: str) -> None:
    if not has_column(db_path, "acceptance_criteria", "skip_note"):
        run_script(db_path, """
            ALTER TABLE acceptance_criteria ADD COLUMN skip_note TEXT;
            PRAGMA user_version = 46;
        """)
    else:
        set_version(db_path, 46)
    _progress("  Migration 46: added 'skip_note' column to acceptance_criteria")


def migrate_47(db_path: str, config_path: str, script_dir: str) -> None:
    """Backfill pillars table from <repo_root>/docs/PILLARS.md when present.

    docs/PILLARS.md is the canonical narrative source; the pillars table is a
    normalized projection used by /investigate, /investigate-directory, and
    /address-issue. When PILLARS.md is absent (target projects), this migration
    is a no-op and the table keeps whatever /tusk-init seeded.
    """
    if get_version(db_path) >= 47:
        return
    conn = db_connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM pillars").fetchone()[0]
    conn.close()
    if count == 0:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
        md_path = os.path.join(repo_root, "docs", "PILLARS.md")
        if os.path.isfile(md_path):
            try:
                subprocess.run(
                    [
                        "python3",
                        os.path.join(script_dir, "tusk-pillars.py"),
                        db_path,
                        config_path,
                        "sync-from-md",
                    ],
                    check=True,
                    capture_output=True,
                )
            except Exception as e:
                print(f"  Warning: PILLARS.md backfill failed: {e}", file=sys.stderr)
    set_version(db_path, 47)
    _progress("  Migration 47: backfilled pillars table from docs/PILLARS.md")


def migrate_48(db_path: str, config_path: str, script_dir: str) -> None:
    """Collapse the multi-entry review config array into a single reviewer object.

    Old schema shape:  a `reviewers` array; each entry carried name, description,
    and optionally a per-entry filter list.
    New schema shape:  a `reviewer` object with name and description.

    Policy: take the first entry of the old array, drop any filter field
    from it, and write it as `review.reviewer`. An empty old array drops
    the key entirely (cmd_start then creates an unassigned review).
    """
    if get_version(db_path) < 48:
        try:
            with open(config_path) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  Warning: skipping migration 48 — could not read {config_path}: {e}", file=sys.stderr)
            set_version(db_path, 48)
            _progress("  Migration 48: collapsed review.reviewers (array) into review.reviewer (object)")
            return

        review = cfg.get("review")
        if isinstance(review, dict) and "reviewers" in review:
            reviewers = review.pop("reviewers")
            if isinstance(reviewers, list) and reviewers and isinstance(reviewers[0], dict):
                first = dict(reviewers[0])
                first.pop("domains", None)
                review["reviewer"] = first
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")
        set_version(db_path, 48)
    else:
        set_version(db_path, 48)
    _progress("  Migration 48: collapsed review.reviewers (array) into review.reviewer (object)")


# ── Migration registry ────────────────────────────────────────────────────────

MIGRATIONS = [
    (1,  migrate_1),
    (2,  migrate_2),
    (3,  migrate_3),
    (4,  migrate_4),
    (5,  migrate_5),
    (6,  migrate_6),
    (7,  migrate_7),
    (8,  migrate_8),
    (9,  migrate_9),
    (10, migrate_10),
    (11, migrate_11),
    (12, migrate_12),
    (13, migrate_13),
    (14, migrate_14),
    (15, migrate_15),
    (16, migrate_16),
    (17, migrate_17),
    (18, migrate_18),
    (19, migrate_19),
    (20, migrate_20),
    (21, migrate_21),
    (22, migrate_22),
    (23, migrate_23),
    (24, migrate_24),
    (25, migrate_25),
    (26, migrate_26),
    (27, migrate_27),
    (28, migrate_28),
    (29, migrate_29),
    (30, migrate_30),
    (31, migrate_31),
    (32, migrate_32),
    (33, migrate_33),
    (34, migrate_34),
    (35, migrate_35),
    (36, migrate_36),
    (37, migrate_37),
    (38, migrate_38),
    (39, migrate_39),
    (40, migrate_40),
    (41, migrate_41),
    (42, migrate_42),
    (43, migrate_43),
    (44, migrate_44),
    (45, migrate_45),
    (46, migrate_46),
    (47, migrate_47),
    (48, migrate_48),
]


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: tusk-migrate.py <db_path> <config_path>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]
    script_dir = os.path.dirname(os.path.abspath(__file__))

    if not os.path.isfile(db_path):
        print(f"No database found at {db_path} — run 'tusk init' first.", file=sys.stderr)
        sys.exit(1)

    current = get_version(db_path)

    for version, func in MIGRATIONS:
        if current < version:
            func(db_path, config_path, script_dir)

    final = get_version(db_path)
    if final == current:
        print(f"Schema is up to date (version {final}).")
    else:
        print(f"Migrated schema from version {current} → {final}.")


if __name__ == "__main__":
    main()

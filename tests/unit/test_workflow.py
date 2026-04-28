"""Unit tests for the workflow column — migration, config validation, insert, update, triggers.

Uses in-memory SQLite DBs and direct module imports — no subprocess.
"""

import importlib.util
import json
import os
import sqlite3
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Load modules ──────────────────────────────────────────────────────────────

_spec_config = importlib.util.spec_from_file_location(
    "tusk_config_tools",
    os.path.join(REPO_ROOT, "bin", "tusk-config-tools.py"),
)
config_tools = importlib.util.module_from_spec(_spec_config)
_spec_config.loader.exec_module(config_tools)

_spec_migrate = importlib.util.spec_from_file_location(
    "tusk_migrate",
    os.path.join(REPO_ROOT, "bin", "tusk-migrate.py"),
)
migrate_mod = importlib.util.module_from_spec(_spec_migrate)
_spec_migrate.loader.exec_module(migrate_mod)

_spec_insert = importlib.util.spec_from_file_location(
    "tusk_task_insert",
    os.path.join(REPO_ROOT, "bin", "tusk-task-insert.py"),
)
insert_mod = importlib.util.module_from_spec(_spec_insert)
_spec_insert.loader.exec_module(insert_mod)

_spec_update = importlib.util.spec_from_file_location(
    "tusk_task_update",
    os.path.join(REPO_ROOT, "bin", "tusk-task-update.py"),
)
update_mod = importlib.util.module_from_spec(_spec_update)
_spec_update.loader.exec_module(update_mod)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_config(path, workflows=None, **overrides):
    """Write a minimal config.json at path."""
    cfg = {
        "domains": [],
        "task_types": ["feature"],
        "statuses": ["To Do", "In Progress", "Done"],
        "priorities": ["High", "Medium", "Low"],
        "closed_reasons": ["completed"],
        "complexity": ["S", "M", "L"],
        "workflows": workflows if workflows is not None else [],
        "agents": {},
    }
    cfg.update(overrides)
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path


# Mirror of bin/tusk's CREATE TABLE tasks block — kept in sync via
# TestTasksSchemaSync below. When a migration adds or renames a tasks column,
# update this constant; the guard will flag the drift if you forget.
_TASKS_TABLE = """
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
    fixes_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    bakeoff_id INTEGER,
    bakeoff_shadow INTEGER NOT NULL DEFAULT 0 CHECK (bakeoff_shadow IN (0, 1))
)
"""


def _make_db_with_workflow(tmp_path):
    """Create a DB file with the tasks table including the workflow column."""
    db_path = str(tmp_path / "tusk" / "tasks.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_TASKS_TABLE)
    conn.execute("""
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER, criterion TEXT, source TEXT DEFAULT 'original',
            is_completed INTEGER DEFAULT 0, is_deferred INTEGER DEFAULT 0,
            deferred_reason TEXT,
            criterion_type TEXT DEFAULT 'manual', verification_spec TEXT,
            verification_result TEXT,
            commit_hash TEXT, committed_at TEXT,
            completed_at TEXT, updated_at TEXT,
            cost_dollars REAL, tokens_in INTEGER, tokens_out INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE task_sessions (
            id INTEGER PRIMARY KEY, task_id INTEGER,
            started_at TEXT, ended_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


# ── Migration tests ───────────────────────────────────────────────────────────

class TestMigration45:
    def test_adds_workflow_column(self, tmp_path):
        """Migration 45 adds a workflow TEXT column to the tasks table."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                summary TEXT NOT NULL,
                status TEXT DEFAULT 'To Do'
            )
        """)
        conn.execute("PRAGMA user_version = 44")
        conn.commit()
        conn.close()

        migrate_mod.migrate_45(db_path, "", "")

        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        assert "workflow" in cols
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 45
        conn.close()

    def test_idempotent_when_column_exists(self, tmp_path):
        """Migration 45 is safe to re-run when the column already exists."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, workflow TEXT)")
        conn.execute("PRAGMA user_version = 44")
        conn.commit()
        conn.close()

        migrate_mod.migrate_45(db_path, "", "")

        conn = sqlite3.connect(db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 45
        conn.close()


# ── Config validation tests ───────────────────────────────────────────────────

class TestConfigValidation:
    def test_workflows_in_known_keys(self):
        """The 'workflows' key is recognized by config validation (not flagged as unknown)."""
        # KNOWN_KEYS is a local var inside cmd_validate, so we test indirectly:
        # a config with only 'workflows' (plus required keys) should not produce
        # an "Unknown config key" error.
        import io
        cfg = {
            "statuses": ["To Do", "In Progress", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed"],
            "workflows": ["planning"],
        }
        config_path = os.path.join(str(self._tmp.name) if hasattr(self, "_tmp") else tempfile.mkdtemp(), "cfg.json")
        with open(config_path, "w") as f:
            json.dump(cfg, f)

        err = StringIO()
        with redirect_stderr(err):
            result = config_tools.cmd_validate(config_path)
        assert result == 0, f"Validation failed: {err.getvalue()}"

    def test_workflows_in_list_fields(self):
        """The 'workflows' key is validated as a list-of-strings field."""
        # If workflows contains a non-string, validation should fail
        cfg = {
            "statuses": ["To Do", "In Progress", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed"],
            "workflows": [123],  # invalid: should be strings
        }
        config_path = os.path.join(tempfile.mkdtemp(), "cfg.json")
        with open(config_path, "w") as f:
            json.dump(cfg, f)

        err = StringIO()
        with redirect_stderr(err):
            result = config_tools.cmd_validate(config_path)
        assert result == 1  # validation error

    def test_config_with_workflows_passes_validation(self, tmp_path):
        """A config containing a valid workflows list passes validation."""
        cfg = {
            "domains": [],
            "task_types": ["feature"],
            "statuses": ["To Do", "In Progress", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed"],
            "complexity": ["S", "M"],
            "workflows": ["planning", "deploy"],
            "agents": {},
        }
        config_path = str(tmp_path / "config.json")
        with open(config_path, "w") as f:
            json.dump(cfg, f)

        result = config_tools.cmd_validate(config_path)
        assert result == 0

    def test_config_with_empty_workflows_passes(self, tmp_path):
        """An empty workflows list passes validation (validation disabled)."""
        cfg = {
            "domains": [],
            "task_types": ["feature"],
            "statuses": ["To Do", "In Progress", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed"],
            "complexity": [],
            "workflows": [],
            "agents": {},
        }
        config_path = str(tmp_path / "config.json")
        with open(config_path, "w") as f:
            json.dump(cfg, f)

        result = config_tools.cmd_validate(config_path)
        assert result == 0


# ── Trigger generation tests ──────────────────────────────────────────────────

class TestTriggerGeneration:
    def test_generates_workflow_triggers_when_configured(self, tmp_path):
        """gen-triggers emits INSERT/UPDATE triggers for workflow when list is non-empty."""
        cfg = {
            "statuses": ["To Do", "In Progress", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed"],
            "workflows": ["planning", "deploy"],
        }
        config_path = str(tmp_path / "config.json")
        with open(config_path, "w") as f:
            json.dump(cfg, f)

        out = StringIO()
        with redirect_stdout(out):
            config_tools.cmd_gen_triggers(config_path)

        output = out.getvalue()
        assert "validate_workflow_insert" in output
        assert "validate_workflow_update" in output
        assert "'planning'" in output
        assert "'deploy'" in output

    def test_no_workflow_triggers_when_empty(self, tmp_path):
        """gen-triggers omits workflow triggers when workflows list is empty."""
        cfg = {
            "statuses": ["To Do", "In Progress", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed"],
            "workflows": [],
        }
        config_path = str(tmp_path / "config.json")
        with open(config_path, "w") as f:
            json.dump(cfg, f)

        out = StringIO()
        with redirect_stdout(out):
            config_tools.cmd_gen_triggers(config_path)

        output = out.getvalue()
        assert "validate_workflow" not in output

    def test_trigger_enforces_workflow_on_insert(self, tmp_path):
        """A workflow validation trigger rejects invalid values on INSERT."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, summary TEXT, workflow TEXT,
                status TEXT DEFAULT 'To Do', priority TEXT DEFAULT 'Medium',
                closed_reason TEXT
            )
        """)

        cfg = {
            "statuses": ["To Do", "In Progress", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed"],
            "workflows": ["planning", "deploy"],
        }
        config_path = str(tmp_path / "config.json")
        with open(config_path, "w") as f:
            json.dump(cfg, f)

        out = StringIO()
        with redirect_stdout(out):
            config_tools.cmd_gen_triggers(config_path)

        conn.executescript(out.getvalue())

        # Valid workflow should succeed
        conn.execute("INSERT INTO tasks (summary, workflow) VALUES ('test', 'planning')")

        # NULL workflow should succeed (nullable)
        conn.execute("INSERT INTO tasks (summary) VALUES ('test2')")

        # Invalid workflow should fail
        with pytest.raises(sqlite3.IntegrityError, match="Invalid workflow"):
            conn.execute("INSERT INTO tasks (summary, workflow) VALUES ('test3', 'bogus')")

        conn.close()

    def test_trigger_enforces_workflow_on_update(self, tmp_path):
        """A workflow validation trigger rejects invalid values on UPDATE."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, summary TEXT, workflow TEXT,
                status TEXT DEFAULT 'To Do', priority TEXT DEFAULT 'Medium',
                closed_reason TEXT
            )
        """)

        cfg = {
            "statuses": ["To Do", "In Progress", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed"],
            "workflows": ["planning"],
        }
        config_path = str(tmp_path / "config.json")
        with open(config_path, "w") as f:
            json.dump(cfg, f)

        out = StringIO()
        with redirect_stdout(out):
            config_tools.cmd_gen_triggers(config_path)
        conn.executescript(out.getvalue())

        conn.execute("INSERT INTO tasks (summary, workflow) VALUES ('task1', 'planning')")

        # Valid update to NULL
        conn.execute("UPDATE tasks SET workflow = NULL WHERE id = 1")

        # Invalid update
        with pytest.raises(sqlite3.IntegrityError, match="Invalid workflow"):
            conn.execute("UPDATE tasks SET workflow = 'bogus' WHERE id = 1")

        conn.close()


# ── Insert tests ──────────────────────────────────────────────────────────────

class TestTaskInsertWorkflow:
    def test_insert_with_workflow(self, tmp_path):
        """task-insert --workflow sets the workflow column."""
        db_path = _make_db_with_workflow(tmp_path)
        config_path = _write_config(str(tmp_path / "config.json"), workflows=["planning", "deploy"])

        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()), \
             patch.object(insert_mod, "run_dupe_check", return_value=None), \
             patch("subprocess.run"):
            result = insert_mod.main([
                db_path, config_path,
                "Test task", "Description",
                "--workflow", "planning",
                "--criteria", "Some criterion",
            ])

        assert result == 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT workflow FROM tasks WHERE id = 1").fetchone()
        assert row["workflow"] == "planning"
        conn.close()

    def test_insert_without_workflow(self, tmp_path):
        """task-insert without --workflow leaves the column NULL."""
        db_path = _make_db_with_workflow(tmp_path)
        config_path = _write_config(str(tmp_path / "config.json"))

        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()), \
             patch.object(insert_mod, "run_dupe_check", return_value=None), \
             patch("subprocess.run"):
            result = insert_mod.main([
                db_path, config_path,
                "Test task", "Description",
                "--criteria", "Some criterion",
            ])

        assert result == 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT workflow FROM tasks WHERE id = 1").fetchone()
        assert row["workflow"] is None
        conn.close()

    def test_insert_invalid_workflow_rejected(self, tmp_path):
        """task-insert rejects an invalid --workflow value."""
        db_path = _make_db_with_workflow(tmp_path)
        config_path = _write_config(str(tmp_path / "config.json"), workflows=["planning"])

        err = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(err):
            result = insert_mod.main([
                db_path, config_path,
                "Test task", "Description",
                "--workflow", "bogus",
                "--criteria", "Some criterion",
            ])

        assert result == 2
        assert "workflow" in err.getvalue().lower()


# ── Update tests ──────────────────────────────────────────────────────────────

class TestTaskUpdateWorkflow:
    def _insert_task(self, db_path, workflow=None):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO tasks (summary, description, status, workflow) "
            "VALUES ('Test', 'Desc', 'To Do', ?)",
            (workflow,),
        )
        conn.commit()
        conn.close()

    def test_update_sets_workflow(self, tmp_path):
        """task-update --workflow sets the workflow column."""
        db_path = _make_db_with_workflow(tmp_path)
        config_path = _write_config(str(tmp_path / "config.json"), workflows=["planning", "deploy"])
        self._insert_task(db_path)

        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()), \
             patch("subprocess.run"):
            result = update_mod.main([
                db_path, config_path,
                "1", "--workflow", "deploy",
            ])

        assert result == 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT workflow FROM tasks WHERE id = 1").fetchone()
        assert row["workflow"] == "deploy"
        conn.close()

    def test_update_clears_workflow_with_empty_string(self, tmp_path):
        """task-update --workflow '' clears workflow to NULL."""
        db_path = _make_db_with_workflow(tmp_path)
        config_path = _write_config(str(tmp_path / "config.json"), workflows=["planning"])
        self._insert_task(db_path, workflow="planning")

        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()), \
             patch("subprocess.run"):
            result = update_mod.main([
                db_path, config_path,
                "1", "--workflow", "",
            ])

        assert result == 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT workflow FROM tasks WHERE id = 1").fetchone()
        assert row["workflow"] is None
        conn.close()

    def test_update_invalid_workflow_rejected(self, tmp_path):
        """task-update rejects an invalid --workflow value."""
        db_path = _make_db_with_workflow(tmp_path)
        config_path = _write_config(str(tmp_path / "config.json"), workflows=["planning"])
        self._insert_task(db_path)

        err = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(err):
            result = update_mod.main([
                db_path, config_path,
                "1", "--workflow", "bogus",
            ])

        assert result == 2
        assert "workflow" in err.getvalue().lower()

    def test_update_workflow_skips_validation_when_empty_config(self, tmp_path):
        """When workflows config is empty, any value is accepted (validation disabled)."""
        db_path = _make_db_with_workflow(tmp_path)
        config_path = _write_config(str(tmp_path / "config.json"), workflows=[])
        self._insert_task(db_path)

        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()), \
             patch("subprocess.run"):
            result = update_mod.main([
                db_path, config_path,
                "1", "--workflow", "anything",
            ])

        assert result == 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT workflow FROM tasks WHERE id = 1").fetchone()
        assert row["workflow"] == "anything"
        conn.close()


# ── Schema sync guard ─────────────────────────────────────────────────────────

def _extract_table_columns(sql_text, table_name):
    """Return the set of column names defined in the CREATE TABLE <table_name> block.

    Mirrors the helper of the same name in tests/unit/test_dashboard_data.py
    and tests/unit/test_skill_run_cancel.py; duplicated so each test file's
    fixture-vs-bin/tusk guard is self-contained.
    """
    import re

    header = re.search(rf"CREATE TABLE {re.escape(table_name)}\s*\(", sql_text, re.IGNORECASE)
    if not header:
        return set()

    body_start = sql_text.index("(", header.start())
    body_lines = []
    for line in sql_text[body_start + 1:].splitlines():
        if line.strip().startswith(")"):
            break
        body_lines.append(line)

    columns = set()
    for line in body_lines:
        line = line.strip().rstrip(",")
        if not line:
            continue
        if re.match(r"(FOREIGN KEY|PRIMARY KEY|UNIQUE|CHECK|CONSTRAINT)\b", line, re.IGNORECASE):
            continue
        col_match = re.match(r"(\w+)", line)
        if col_match:
            columns.add(col_match.group(1).lower())
    return columns


class TestTasksSchemaSync:
    """Guard against drift between _TASKS_TABLE fixture and bin/tusk CREATE TABLE tasks."""

    def test_fixture_matches_bin_tusk(self):
        """Fail if any column is present in one definition but absent from the other.

        Mirrors TestTaskSessionsSchemaSync / TestSkillRunsSchemaSync — catches
        future migrations that add columns to tasks without updating the
        _TASKS_TABLE test fixture. TASK-82's migration 55 added fixes_task_id
        to bin/tusk but _make_db_with_workflow silently drifted, so unrelated
        unit tests began failing mid-commit with 'no such column: fixes_task_id'.
        """
        tusk_path = os.path.join(REPO_ROOT, "bin", "tusk")
        with open(tusk_path) as f:
            tusk_sql = f.read()

        tusk_cols = _extract_table_columns(tusk_sql, "tasks")
        fixture_cols = _extract_table_columns(_TASKS_TABLE, "tasks")

        assert tusk_cols, "Could not find CREATE TABLE tasks in bin/tusk"
        assert fixture_cols, "Could not find CREATE TABLE tasks in _TASKS_TABLE fixture"

        missing_from_fixture = tusk_cols - fixture_cols
        extra_in_fixture = fixture_cols - tusk_cols

        assert not missing_from_fixture, (
            f"tasks columns in bin/tusk missing from _TASKS_TABLE fixture: {sorted(missing_from_fixture)}. "
            "Update _TASKS_TABLE in tests/unit/test_workflow.py to match."
        )
        assert not extra_in_fixture, (
            f"tasks columns in _TASKS_TABLE fixture not in bin/tusk: {sorted(extra_in_fixture)}. "
            "Update _TASKS_TABLE in tests/unit/test_workflow.py to match."
        )

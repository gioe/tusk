"""Unit coverage for ``tusk task-import`` JSON parsing and dry-run validation."""

import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec_import = importlib.util.spec_from_file_location(
    "tusk_task_import",
    os.path.join(REPO_ROOT, "bin", "tusk-task-import.py"),
)
import_mod = importlib.util.module_from_spec(_spec_import)
sys.modules[_spec_import.name] = import_mod
_spec_import.loader.exec_module(import_mod)


def _write_config(path, **overrides):
    cfg = {
        "domains": ["cli", "docs"],
        "task_types": ["feature", "bug"],
        "statuses": ["To Do", "In Progress", "Done"],
        "priorities": ["High", "Medium", "Low"],
        "closed_reasons": ["completed"],
        "complexity": ["S", "M", "L"],
        "workflows": ["planning"],
        "criterion_types": ["manual", "code", "test", "file"],
        "agents": {"codex": {}},
    }
    cfg.update(overrides)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return str(path)


def _make_db(tmp_path):
    db_path = tmp_path / "tusk" / "tasks.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
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
            not_before TEXT,
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
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            criterion TEXT,
            source TEXT DEFAULT 'original',
            is_completed INTEGER DEFAULT 0,
            is_deferred INTEGER DEFAULT 0,
            deferred_reason TEXT,
            criterion_type TEXT DEFAULT 'manual',
            verification_spec TEXT,
            verification_result TEXT,
            commit_hash TEXT,
            committed_at TEXT,
            completed_at TEXT,
            updated_at TEXT,
            cost_dollars REAL,
            tokens_in INTEGER,
            tokens_out INTEGER
        );
        CREATE TABLE task_dependencies (
            task_id INTEGER NOT NULL,
            depends_on_id INTEGER NOT NULL,
            relationship_type TEXT DEFAULT 'blocks',
            PRIMARY KEY (task_id, depends_on_id)
        );
        CREATE TABLE objectives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            closed_at TEXT
        );
        CREATE TABLE objective_tasks (
            objective_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            relationship_type TEXT NOT NULL DEFAULT 'contributes_to',
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (objective_id, task_id)
        );
        CREATE TABLE task_scope (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            source TEXT NOT NULL,
            reason TEXT,
            locked_at TEXT,
            locked_by TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()
    return str(db_path)


def _run_import(db_path, config_path, argv, *, dupes=None):
    out = StringIO()
    err = StringIO()
    with redirect_stdout(out), redirect_stderr(err), \
            patch.object(import_mod, "run_dupe_check", side_effect=dupes or (lambda summary, domain: None)), \
            patch("subprocess.run"):
        code = import_mod.main([db_path, config_path, *argv])
    payload = json.loads(out.getvalue()) if out.getvalue().strip() else None
    return code, payload, err.getvalue()


def test_malformed_json_reports_validation_error(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    plan = tmp_path / "tasks.json"
    plan.write_text('{"tasks": [', encoding="utf-8")

    code, payload, err = _run_import(db_path, config_path, ["--file", str(plan), "--dry-run"])

    assert code == 2
    assert payload["failed"]["0"]["errors"][0]["field"] == "$"
    assert "malformed JSON" in payload["failed"]["0"]["errors"][0]["message"]
    assert err == ""


def test_schema_errors_are_keyed_by_input_index_and_key(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "bad",
                    "summary": "",
                    "description": "Description",
                    "priority": "Urgent",
                    "domain": "mobile",
                    "criteria": [{"text": "Needs spec", "type": "test"}],
                    "duplicate_policy": "maybe",
                    "depends_on": ["missing"],
                }
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan), "--dry-run"])

    assert code == 2
    assert payload["created"] == {}
    assert payload["skipped"] == {}
    failed = payload["failed"]["0"]
    assert failed["key"] == "bad"
    messages = {error["field"]: error["message"] for error in failed["errors"]}
    assert messages["summary"] == "summary is required"
    assert "Invalid priority" in messages["priority"]
    assert "Invalid domain" in messages["domain"]
    assert messages["criteria[0].spec"] == "spec required for type 'test'"
    assert "duplicate_policy" in messages["duplicate_policy"]
    assert messages["depends_on[0]"] == "unknown task key 'missing'"


def test_dry_run_validates_without_writing_rows_and_reports_duplicates(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tasks (summary, description, status) VALUES (?, ?, 'To Do')",
        ("Existing dependency", "Already there"),
    )
    conn.commit()
    conn.close()
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "base",
                    "summary": "Base import task",
                    "description": "Description",
                    "domain": "cli",
                    "criteria": ["Manual criterion"],
                    "depends_on": [{"id": 1, "type": "contingent"}],
                },
                {
                    "key": "dupe",
                    "summary": "Duplicate import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "duplicate_policy": "skip",
                },
            ]
        }),
        encoding="utf-8",
    )

    def dupes(summary, domain):
        if summary == "Duplicate import task":
            return {"id": 42, "summary": "Matched", "similarity": 0.91}
        return None

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan), "--dry-run"], dupes=dupes)

    assert code == 0
    assert payload["created"]["0"] == {"key": "base", "dry_run": True}
    assert payload["skipped"]["1"]["key"] == "dupe"
    assert payload["skipped"]["1"]["reason"] == "duplicate"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM acceptance_criteria").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0] == 0
    conn.close()


def test_import_accepts_json_from_stdin_and_writes_rows(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(json.dumps({
            "tasks": [
                {
                    "key": "base",
                    "summary": "Base import task",
                    "description": "Description",
                    "priority": "High",
                    "domain": "cli",
                    "criteria": ["Manual criterion"],
                },
                {
                    "key": "child",
                    "summary": "Child import task",
                    "description": "Description",
                    "criteria": [{"text": "Run unit test", "type": "test", "spec": "pytest"}],
                    "depends_on": ["base"],
                },
            ]
        })),
    )

    code, payload, _err = _run_import(db_path, config_path, ["--stdin"])

    assert code == 0
    assert payload["created"]["0"]["key"] == "base"
    assert payload["created"]["0"]["task_id"] == 1
    assert payload["created"]["1"]["key"] == "child"
    assert payload["created"]["1"]["task_id"] == 2
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM acceptance_criteria").fetchone()[0] == 2
    assert conn.execute(
        "SELECT relationship_type FROM task_dependencies WHERE task_id = 2 AND depends_on_id = 1"
    ).fetchone()[0] == "blocks"
    conn.close()


def test_import_resolves_existing_task_identifier_string_dependencies(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tasks (summary, description, status) VALUES (?, ?, 'To Do')",
        ("Existing prerequisite", "Already in backlog"),
    )
    conn.commit()
    conn.close()
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "child",
                    "summary": "Child with TASK ref",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "depends_on": ["TASK-1"],
                }
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan)])

    assert code == 0
    assert payload["created"]["0"]["task_id"] == 2
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT relationship_type FROM task_dependencies WHERE task_id = 2 AND depends_on_id = 1"
    ).fetchone()[0] == "blocks"
    conn.close()


def test_import_rejects_invalid_dependency_identifier_objects(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "child",
                    "summary": "Child with bad TASK ref",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "depends_on": [{"id": "TASK-nope"}],
                }
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan)])

    assert code == 2
    assert payload["created"] == {}
    assert payload["failed"]["0"]["key"] == "child"
    assert payload["failed"]["0"]["errors"] == [
        {
            "field": "depends_on[0]",
            "message": "dependency id must be a positive integer or TASK-N",
        }
    ]
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0] == 0
    conn.close()


def test_import_rejects_cyclic_local_key_dependencies_and_rolls_back(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "a",
                    "summary": "Cycle A",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "depends_on": ["b"],
                },
                {
                    "key": "b",
                    "summary": "Cycle B",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "depends_on": ["a"],
                },
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan)])

    assert code == 2
    assert payload["created"] == {}
    assert payload["failed"]["1"]["key"] == "b"
    assert payload["failed"]["1"]["errors"] == [
        {
            "field": "depends_on[0]",
            "message": "dependency would create a cycle",
        }
    ]
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0] == 0
    conn.close()


def test_import_links_created_tasks_to_existing_objectives(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO objectives (summary) VALUES (?)", ("Primary objective",))
    conn.execute("INSERT INTO objectives (summary) VALUES (?)", ("Contributing objective",))
    conn.execute("INSERT INTO objectives (summary) VALUES (?)", ("Follow-up objective",))
    conn.commit()
    conn.close()
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "primary",
                    "summary": "Primary objective linked import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "objectives": [
                        {"id": "OBJ-1", "type": "primary"},
                    ],
                },
                {
                    "key": "contributes",
                    "summary": "Contributing objective linked import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "objectives": [2],
                },
                {
                    "key": "follow-up",
                    "summary": "Follow-up objective linked import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "objectives": [
                        {"id": 3, "type": "follow_up"},
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan)])

    assert code == 0
    assert payload["created"]["0"]["task_id"] == 1
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT objective_id, task_id, relationship_type FROM objective_tasks ORDER BY objective_id"
    ).fetchall()
    assert rows == [
        (1, 1, "primary"),
        (2, 2, "contributes_to"),
        (3, 3, "follow_up"),
    ]
    conn.close()


def test_import_rejects_missing_objective_links_and_rolls_back(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "missing-link",
                    "summary": "Missing objective link import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "objectives": [{"id": "OBJ-999", "type": "primary"}],
                }
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan)])

    assert code == 2
    assert payload["created"] == {}
    assert payload["failed"]["0"]["key"] == "missing-link"
    assert payload["failed"]["0"]["errors"] == [
        {
            "field": "objectives[0].id",
            "message": "objective OBJ-999 does not exist",
        }
    ]
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM objective_tasks").fetchone()[0] == 0
    conn.close()


def test_import_rejects_invalid_objective_types_and_rolls_back(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO objectives (summary) VALUES (?)", ("Objective",))
    conn.commit()
    conn.close()
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "bad-link",
                    "summary": "Bad objective link import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "objectives": [{"id": "OBJ-1", "type": "owner"}],
                }
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan)])

    assert code == 2
    assert payload["created"] == {}
    assert payload["failed"]["0"]["key"] == "bad-link"
    messages = {error["field"]: error["message"] for error in payload["failed"]["0"]["errors"]}
    assert messages["objectives[0].type"] == "type must be primary, contributes_to, or follow_up"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM objective_tasks").fetchone()[0] == 0
    conn.close()


def test_import_materializes_task_insert_metadata_criteria_and_scope(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tasks (summary, description, status) VALUES (?, ?, 'Done')",
        ("Original bug", "Already fixed"),
    )
    conn.commit()
    conn.close()
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "full",
                    "summary": "Imported full metadata task",
                    "description": "Update bin/tusk-task-import.py",
                    "priority": "high",
                    "domain": "cli",
                    "task_type": "bug",
                    "assignee": "codex",
                    "complexity": "L",
                    "workflow": "planning",
                    "expires_in": 3,
                    "not_before": "2026-08-01T12:00:00Z",
                    "fixes_task_id": 1,
                    "scope": ["bin/tusk-task-import.py"],
                    "creates": ["bin/import-generated.py"],
                    "unbounded": True,
                    "criteria": [
                        "Manual criterion",
                        {
                            "text": "Run import unit test",
                            "type": "test",
                            "spec": "python3 -m pytest tests/unit/test_task_import.py -q",
                        },
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan)])

    assert code == 0
    assert payload["created"]["0"]["key"] == "full"
    assert payload["created"]["0"]["task_id"] == 2
    assert payload["created"]["0"]["criteria_ids"] == [1, 2]
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT summary, priority, domain, task_type, assignee, complexity,
               workflow, expires_at, not_before, fixes_task_id
        FROM tasks WHERE id = 2
        """
    ).fetchone()
    assert row[:7] == (
        "Imported full metadata task",
        "High",
        "cli",
        "bug",
        "codex",
        "L",
        "planning",
    )
    assert row[7] is not None
    assert row[8] == "2026-08-01 12:00:00"
    assert row[9] == 1
    criteria = conn.execute(
        """
        SELECT criterion, criterion_type, verification_spec
        FROM acceptance_criteria
        WHERE task_id = 2
        ORDER BY id
        """
    ).fetchall()
    assert criteria == [
        ("Manual criterion", "manual", None),
        (
            "Run import unit test",
            "test",
            "python3 -m pytest tests/unit/test_task_import.py -q",
        ),
    ]
    scope_rows = conn.execute(
        "SELECT pattern, source FROM task_scope WHERE task_id = 2 ORDER BY id"
    ).fetchall()
    assert scope_rows == [
        ("bin/tusk-task-import.py", "operator_declared"),
        ("bin/import-generated.py", "creates"),
        ("**", "unbounded"),
    ]
    conn.close()


def test_best_effort_records_created_skipped_and_failed_outcomes(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "created",
                    "summary": "Created import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                },
                {
                    "key": "failed",
                    "summary": "Duplicate failure import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                },
                {
                    "key": "skipped",
                    "summary": "Duplicate skip import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                    "duplicate_policy": "skip",
                },
            ]
        }),
        encoding="utf-8",
    )

    def dupes(summary, domain):
        if summary == "Duplicate failure import task":
            return {"id": 41, "summary": "Existing fail", "similarity": 0.9}
        if summary == "Duplicate skip import task":
            return {"id": 42, "summary": "Existing skip", "similarity": 0.91}
        return None

    code, payload, _err = _run_import(
        db_path,
        config_path,
        ["--file", str(plan), "--best-effort"],
        dupes=dupes,
    )

    assert code == 2
    assert payload["created"]["0"]["key"] == "created"
    assert payload["created"]["0"]["task_id"] == 1
    assert payload["failed"]["1"]["key"] == "failed"
    assert payload["failed"]["1"]["errors"] == [
        {"field": "duplicate_policy", "message": "duplicate of TASK-41"}
    ]
    assert payload["skipped"]["2"]["key"] == "skipped"
    assert payload["skipped"]["2"]["reason"] == "duplicate"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT summary FROM tasks").fetchall() == [("Created import task",)]
    assert conn.execute("SELECT COUNT(*) FROM acceptance_criteria").fetchone()[0] == 1
    conn.close()


def test_atomic_mode_rolls_back_full_batch_when_later_task_fails(tmp_path):
    db_path = _make_db(tmp_path)
    config_path = _write_config(tmp_path / "config.json")
    plan = tmp_path / "tasks.json"
    plan.write_text(
        json.dumps({
            "tasks": [
                {
                    "key": "first",
                    "summary": "First atomic import task",
                    "description": "Description",
                    "criteria": ["Manual criterion"],
                },
                {
                    "key": "bad",
                    "summary": "Bad atomic import task",
                    "description": "Description",
                    "fixes_task_id": 999,
                    "criteria": ["Manual criterion"],
                },
            ]
        }),
        encoding="utf-8",
    )

    code, payload, _err = _run_import(db_path, config_path, ["--file", str(plan)])

    assert code == 2
    assert payload["created"] == {}
    assert payload["failed"]["1"]["key"] == "bad"
    assert payload["failed"]["1"]["errors"] == [
        {
            "field": "$",
            "message": "--fixes-task-id 999 does not reference an existing task",
        }
    ]
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM acceptance_criteria").fetchone()[0] == 0
    conn.close()

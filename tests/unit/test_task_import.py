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

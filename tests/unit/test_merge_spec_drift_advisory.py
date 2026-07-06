"""Unit tests for merge-time spec drift advisories."""

import importlib.util
import os
import sqlite3
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _load_real_git_helpers():
    spec = importlib.util.spec_from_file_location(
        "tusk_git_helpers", os.path.join(REPO_ROOT, "bin", "tusk-git-helpers.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.get_connection = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    real_git_helpers = _load_real_git_helpers()

    def _load(name):
        if name == "tusk-git-helpers":
            return real_git_helpers
        return db_lib_mock

    tusk_loader_mock.load.side_effect = _load
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT,
            description TEXT
        );
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            criterion TEXT,
            criterion_type TEXT,
            verification_spec TEXT,
            verification_result TEXT,
            is_completed INTEGER DEFAULT 0
        );
        CREATE TABLE task_scope (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            pattern TEXT,
            source TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, summary, description) VALUES (1, 'merge advisory', '')"
    )
    return conn


def test_no_advisory_when_evidence_and_scope_match():
    mod = _load_module()
    conn = _conn()
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(id, task_id, criterion, criterion_type, verification_result, is_completed) "
        "VALUES (10, 1, 'code verified', 'code', '{\"status\":\"passed\"}', 1)"
    )
    conn.execute(
        "INSERT INTO task_scope (task_id, pattern, source) VALUES (1, 'bin/tusk-merge.py', 'manual')"
    )

    lines = mod._spec_drift_advisory_lines(conn, 1, ["bin/tusk-merge.py"])

    assert lines == []


def test_advisory_flags_missing_evidence_and_out_of_scope_files():
    mod = _load_module()
    conn = _conn()
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(id, task_id, criterion, criterion_type, verification_result, is_completed) "
        "VALUES (11, 1, 'unit test proves drift advisory', 'test', NULL, 1)"
    )
    conn.execute(
        "INSERT INTO task_scope (task_id, pattern, source) VALUES (1, 'bin/tusk-merge.py', 'manual')"
    )

    lines = mod._spec_drift_advisory_lines(
        conn, 1, ["bin/tusk-merge.py", "docs/unrelated.md"]
    )

    output = "\n".join(lines)
    assert "TASK-1 may have spec drift" in output
    assert "[11] test: unit test proves drift advisory" in output
    assert "docs/unrelated.md" in output


def test_unbounded_scope_suppresses_out_of_scope_file_warning():
    mod = _load_module()
    conn = _conn()
    conn.execute(
        "INSERT INTO task_scope (task_id, pattern, source) VALUES (1, '*', 'unbounded')"
    )

    lines = mod._spec_drift_advisory_lines(conn, 1, ["docs/unrelated.md"])

    assert lines == []

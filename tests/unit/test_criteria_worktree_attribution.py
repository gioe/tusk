"""Regression tests for criteria commit attribution from task worktrees."""

import argparse
import os
import sqlite3
import subprocess

from tests.unit.test_criteria_done import criteria_mod


def _init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT,
            status TEXT
        );
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            criterion TEXT NOT NULL,
            criterion_type TEXT DEFAULT 'manual',
            verification_spec TEXT,
            is_completed INTEGER DEFAULT 0,
            completed_at TEXT,
            verification_result TEXT,
            commit_hash TEXT,
            committed_at TEXT,
            skip_note TEXT,
            updated_at TEXT
        );
        CREATE TABLE task_workspaces (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            branch TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute("INSERT INTO tasks (id, summary, status) VALUES (100, 'task', 'In Progress')")
    conn.execute(
        "INSERT INTO acceptance_criteria (id, task_id, criterion, criterion_type) "
        "VALUES (1, 100, 'criterion', 'manual')"
    )
    conn.commit()
    conn.close()


def _args(*criterion_ids):
    return argparse.Namespace(
        criterion_ids=list(criterion_ids),
        skip_verify=True,
        allow_shared_commit=False,
        batch=False,
        note=None,
    )


def _commit_hash(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT commit_hash FROM acceptance_criteria WHERE id = 1"
        ).fetchone()[0]
    finally:
        conn.close()


def test_done_uses_recorded_task_worktree_head_when_available(tmp_path, monkeypatch):
    mod = criteria_mod
    db_path = tmp_path / "tasks.db"
    workspace = tmp_path / "TASK-100-workspace"
    workspace.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (100, 'feature/TASK-100-work', ?)",
            (str(workspace),),
        )
        conn.commit()
    finally:
        conn.close()

    calls = []

    def fake_check_output(args, stderr=None, encoding=None, cwd=None):
        calls.append((args, cwd))
        if args == ["git", "rev-parse", "--short", "HEAD"] and cwd == str(workspace):
            return "work123\n"
        if args == ["git", "log", "-1", "--format=%cI", "HEAD"] and cwd == str(workspace):
            return "2026-05-09T10:00:00-04:00\n"
        if args == ["git", "rev-parse", "--short", "HEAD"]:
            return "main999\n"
        if args == ["git", "log", "-1", "--format=%B", "HEAD"]:
            return "[TASK-100] Worktree commit\n"
        if args == ["git", "log", "-1", "--format=%cI", "HEAD"]:
            return "2026-05-09T09:00:00-04:00\n"
        raise AssertionError(f"unexpected check_output call: {args} cwd={cwd}")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(mod, "_has_new_commits_over_default", lambda: True)
    monkeypatch.setattr(mod, "capture_criterion_cost", lambda *a, **k: None)

    rc = mod.cmd_done(_args(1), str(db_path), {})

    assert rc == 0
    assert _commit_hash(db_path) == "work123"
    assert (["git", "rev-parse", "--short", "HEAD"], str(workspace)) in calls


def test_done_uses_current_checkout_when_no_recorded_task_worktree(tmp_path, monkeypatch):
    mod = criteria_mod
    db_path = tmp_path / "tasks.db"
    _init_db(db_path)

    def fake_check_output(args, stderr=None, encoding=None, cwd=None):
        if args == ["git", "rev-parse", "--short", "HEAD"]:
            assert cwd is None
            return "main999\n"
        if args == ["git", "log", "-1", "--format=%cI", "HEAD"]:
            assert cwd is None
            return "2026-05-09T09:00:00-04:00\n"
        if args == ["git", "log", "-1", "--format=%B", "HEAD"]:
            return "[TASK-100] Main checkout commit\n"
        raise AssertionError(f"unexpected check_output call: {args} cwd={cwd}")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(mod, "_has_new_commits_over_default", lambda: True)
    monkeypatch.setattr(mod, "capture_criterion_cost", lambda *a, **k: None)

    rc = mod.cmd_done(_args(1), str(db_path), {})

    assert rc == 0
    assert _commit_hash(db_path) == "main999"

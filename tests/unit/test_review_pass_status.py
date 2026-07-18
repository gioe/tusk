"""Focused tests for review pass retry state and verdict pass preservation."""

import argparse
import importlib.util
import json
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "tusk_review_pass_status", os.path.join(REPO_ROOT, "bin", "tusk-review.py")
)
review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review)


def _make_db(tmp_path, *, max_passes=3):
    db_path = str(tmp_path / "tasks.db")
    config_path = str(tmp_path / "config.json")
    with open(config_path, "w", encoding="utf-8") as config_file:
        json.dump({"review": {"max_passes": max_passes}}, config_file)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT);
        CREATE TABLE code_reviews (
            id INTEGER PRIMARY KEY, task_id INTEGER, reviewer TEXT, status TEXT,
            review_pass INTEGER, note TEXT, model TEXT, cost_dollars REAL,
            tokens_in INTEGER, tokens_out INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE review_comments (
            id INTEGER PRIMARY KEY, review_id INTEGER, category TEXT,
            resolution TEXT
        );
        INSERT INTO tasks (id, summary) VALUES (1, 'task');
        """
    )
    conn.commit()
    conn.close()
    return db_path, config_path


def _insert_review(db_path, review_id, review_pass, *, status="approved"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
        " VALUES (?, 1, 'reviewer', ?, ?)",
        (review_id, status, review_pass),
    )
    conn.commit()
    conn.close()


def _insert_must_fix(db_path, comment_id, review_id, resolution):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO review_comments (id, review_id, category, resolution)"
        " VALUES (?, ?, 'must_fix', ?)",
        (comment_id, review_id, resolution),
    )
    conn.commit()
    conn.close()


def _pass_status(db_path, config_path, capsys):
    assert review.cmd_pass_status(argparse.Namespace(task_id=1), db_path, config_path) == 0
    return json.loads(capsys.readouterr().out)


def test_resolved_must_fix_on_latest_review_allows_verification(tmp_path, capsys):
    db_path, config_path = _make_db(tmp_path)
    _insert_review(db_path, 1, 1)
    _insert_must_fix(db_path, 1, 1, "fixed")

    result = _pass_status(db_path, config_path, capsys)

    assert result == {
        "current_pass": 1, "max_passes": 3, "can_retry": True,
        "open_must_fix": 0, "fixed_must_fix": 1,
    }


def test_clean_latest_review_does_not_retry(tmp_path, capsys):
    db_path, config_path = _make_db(tmp_path)
    _insert_review(db_path, 1, 1)

    result = _pass_status(db_path, config_path, capsys)

    assert result["can_retry"] is False
    assert result["open_must_fix"] == result["fixed_must_fix"] == 0


def test_max_pass_blocks_retry_even_with_fixed_finding(tmp_path, capsys):
    db_path, config_path = _make_db(tmp_path, max_passes=2)
    _insert_review(db_path, 1, 2)
    _insert_must_fix(db_path, 1, 1, "fixed")

    result = _pass_status(db_path, config_path, capsys)

    assert result["current_pass"] == result["max_passes"] == 2
    assert result["fixed_must_fix"] == 1
    assert result["can_retry"] is False


def test_prior_fixed_finding_does_not_retrigger_after_clean_latest_review(tmp_path, capsys):
    db_path, config_path = _make_db(tmp_path)
    _insert_review(db_path, 1, 1)
    _insert_must_fix(db_path, 1, 1, "fixed")
    _insert_review(db_path, 2, 2)

    result = _pass_status(db_path, config_path, capsys)

    assert result["current_pass"] == 2
    assert result["fixed_must_fix"] == 0
    assert result["can_retry"] is False


def test_approve_preserves_assigned_review_pass(tmp_path):
    db_path, _ = _make_db(tmp_path)
    _insert_review(db_path, 1, 2, status="pending")

    args = argparse.Namespace(review_id=1, note=None, model=None, skip_cost=True)
    assert review.cmd_approve(args, db_path) == 0

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT status, review_pass FROM code_reviews").fetchone() == ("approved", 2)
    conn.close()


def test_request_changes_preserves_assigned_review_pass(tmp_path):
    db_path, _ = _make_db(tmp_path)
    _insert_review(db_path, 1, 2, status="pending")

    args = argparse.Namespace(review_id=1, note=None, model=None, skip_cost=True)
    assert review.cmd_request_changes(args, db_path) == 0

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT status, review_pass FROM code_reviews").fetchone() == ("changes_requested", 2)
    conn.close()

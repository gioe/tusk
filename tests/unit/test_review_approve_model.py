"""Unit tests for the --model flag on tusk review approve / request-changes.

Exercises cmd_approve and cmd_request_changes end-to-end against a real
on-disk SQLite DB (the functions use sqlite3.connect(db_path) internally).
"""

import argparse
import importlib.util
import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_review",
    os.path.join(REPO_ROOT, "bin", "tusk-review.py"),
)
review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review)


def _make_db(tmp_path):
    """Create a tasks.db with the full code_reviews schema (including model)."""
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT
        );
        CREATE TABLE code_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            reviewer TEXT,
            status TEXT DEFAULT 'pending',
            review_pass INTEGER DEFAULT 1,
            diff_summary TEXT,
            cost_dollars REAL,
            tokens_in INTEGER,
            tokens_out INTEGER,
            agent_name TEXT,
            model TEXT,
            note TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        INSERT INTO tasks (id, summary) VALUES (1, 'sample task');
        INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)
          VALUES (1, 1, 'alice', 'pending', 1);
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _fetch(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, note, model FROM code_reviews WHERE id = 1").fetchone()
    conn.close()
    return row


class TestApproveModel:
    def test_approve_with_model_persists(self, tmp_path):
        db = _make_db(tmp_path)
        args = argparse.Namespace(review_id=1, note=None, model="claude-opus-4-7")

        assert review.cmd_approve(args, db) == 0

        row = _fetch(db)
        assert row["status"] == "approved"
        assert row["model"] == "claude-opus-4-7"
        assert row["note"] is None

    def test_approve_with_model_and_note_persists_both(self, tmp_path):
        db = _make_db(tmp_path)
        args = argparse.Namespace(review_id=1, note="LGTM", model="claude-sonnet-4-6")

        assert review.cmd_approve(args, db) == 0

        row = _fetch(db)
        assert row["status"] == "approved"
        assert row["note"] == "LGTM"
        assert row["model"] == "claude-sonnet-4-6"

    def test_approve_without_model_leaves_model_null(self, tmp_path):
        """Backwards compatibility: existing callers without --model still work."""
        db = _make_db(tmp_path)
        args = argparse.Namespace(review_id=1, note=None, model=None)

        assert review.cmd_approve(args, db) == 0

        row = _fetch(db)
        assert row["status"] == "approved"
        assert row["model"] is None

    def test_approve_without_model_does_not_clobber_existing(self, tmp_path):
        """A subsequent approve without --model should not null out a prior model value."""
        db = _make_db(tmp_path)
        # First approve sets model
        review.cmd_approve(
            argparse.Namespace(review_id=1, note=None, model="claude-opus-4-7"),
            db,
        )
        # Second approve without --model
        review.cmd_approve(
            argparse.Namespace(review_id=1, note="second pass", model=None),
            db,
        )

        row = _fetch(db)
        assert row["model"] == "claude-opus-4-7"
        assert row["note"] == "second pass"


class TestRequestChangesModel:
    def test_request_changes_with_model_persists(self, tmp_path):
        db = _make_db(tmp_path)
        args = argparse.Namespace(review_id=1, note=None, model="claude-opus-4-7")

        assert review.cmd_request_changes(args, db) == 0

        row = _fetch(db)
        assert row["status"] == "changes_requested"
        assert row["model"] == "claude-opus-4-7"

    def test_request_changes_without_model_leaves_model_null(self, tmp_path):
        db = _make_db(tmp_path)
        args = argparse.Namespace(review_id=1, note=None, model=None)

        assert review.cmd_request_changes(args, db) == 0

        row = _fetch(db)
        assert row["status"] == "changes_requested"
        assert row["model"] is None


class TestRequestChangesNote:
    def test_request_changes_with_note_persists(self, tmp_path):
        db = _make_db(tmp_path)
        args = argparse.Namespace(review_id=1, note="2 findings to fix", model=None)

        assert review.cmd_request_changes(args, db) == 0

        row = _fetch(db)
        assert row["status"] == "changes_requested"
        assert row["note"] == "2 findings to fix"
        assert row["model"] is None

    def test_request_changes_with_note_and_model_persists_both(self, tmp_path):
        db = _make_db(tmp_path)
        args = argparse.Namespace(
            review_id=1, note="needs more tests", model="claude-opus-4-7"
        )

        assert review.cmd_request_changes(args, db) == 0

        row = _fetch(db)
        assert row["status"] == "changes_requested"
        assert row["note"] == "needs more tests"
        assert row["model"] == "claude-opus-4-7"

    def test_request_changes_without_note_does_not_clobber_existing(self, tmp_path):
        """A subsequent request-changes without --note should not null out a prior note value."""
        db = _make_db(tmp_path)
        review.cmd_request_changes(
            argparse.Namespace(review_id=1, note="first pass", model=None),
            db,
        )
        review.cmd_request_changes(
            argparse.Namespace(review_id=1, note=None, model=None),
            db,
        )

        row = _fetch(db)
        assert row["note"] == "first pass"

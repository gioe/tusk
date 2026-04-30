"""Unit tests for cost/token capture on tusk review approve / request-changes.

Cost is auto-computed from the transcript window between the row's
`created_at` and now. These tests stub the auto-compute hook to keep them
deterministic and verify both the auto-compute path and the explicit
override flags populate the right columns.
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


def _fetch_cost(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, cost_dollars, tokens_in, tokens_out, model FROM code_reviews WHERE id = 1"
    ).fetchone()
    conn.close()
    return row


class _StubCompute:
    """Replace `_compute_review_cost_from_window` for deterministic tests."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def __call__(self, created_at):
        self.calls += 1
        return self.payload


def _ns(**overrides):
    """Build a Namespace pre-populated with all approve/request-changes args."""
    base = {
        "review_id": 1,
        "note": None,
        "model": None,
        "cost_dollars": None,
        "tokens_in": None,
        "tokens_out": None,
        "skip_cost": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestApproveAutoComputeFromTranscript:
    def test_approve_writes_auto_computed_cost(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        stub = _StubCompute({
            "cost_dollars": 0.0421,
            "tokens_in": 12345,
            "tokens_out": 678,
            "model": "claude-opus-4-7",
        })
        monkeypatch.setattr(review, "_compute_review_cost_from_window", stub)

        assert review.cmd_approve(_ns(model="claude-opus-4-7"), db) == 0

        row = _fetch_cost(db)
        assert row["status"] == "approved"
        assert row["cost_dollars"] == 0.0421
        assert row["tokens_in"] == 12345
        assert row["tokens_out"] == 678
        assert stub.calls == 1

    def test_approve_leaves_cost_null_when_no_transcript(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(review, "_compute_review_cost_from_window", _StubCompute(None))

        assert review.cmd_approve(_ns(), db) == 0

        row = _fetch_cost(db)
        assert row["status"] == "approved"
        assert row["cost_dollars"] is None
        assert row["tokens_in"] is None
        assert row["tokens_out"] is None


class TestApproveExplicitOverrides:
    def test_explicit_cost_dollars_replaces_auto_computed(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        # Auto-compute would return these values, but explicit flag must win.
        monkeypatch.setattr(
            review,
            "_compute_review_cost_from_window",
            _StubCompute({"cost_dollars": 9.99, "tokens_in": 1, "tokens_out": 2, "model": "x"}),
        )

        args = _ns(cost_dollars=0.05, tokens_in=2000, tokens_out=300)
        assert review.cmd_approve(args, db) == 0

        row = _fetch_cost(db)
        assert row["cost_dollars"] == 0.05
        assert row["tokens_in"] == 2000
        assert row["tokens_out"] == 300

    def test_partial_explicit_mixes_with_auto_computed(self, tmp_path, monkeypatch):
        """Explicit --cost-dollars only — tokens still auto-computed."""
        db = _make_db(tmp_path)
        monkeypatch.setattr(
            review,
            "_compute_review_cost_from_window",
            _StubCompute({"cost_dollars": 0.10, "tokens_in": 500, "tokens_out": 50, "model": "x"}),
        )

        args = _ns(cost_dollars=0.07)
        assert review.cmd_approve(args, db) == 0

        row = _fetch_cost(db)
        assert row["cost_dollars"] == 0.07
        assert row["tokens_in"] == 500
        assert row["tokens_out"] == 50


class TestApproveSkipCost:
    def test_skip_cost_does_not_invoke_auto_compute(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        stub = _StubCompute({"cost_dollars": 9.99, "tokens_in": 1, "tokens_out": 2, "model": "x"})
        monkeypatch.setattr(review, "_compute_review_cost_from_window", stub)

        args = _ns(skip_cost=True)
        assert review.cmd_approve(args, db) == 0

        row = _fetch_cost(db)
        assert row["status"] == "approved"
        assert row["cost_dollars"] is None
        assert stub.calls == 0

    def test_skip_cost_still_honors_explicit_values(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        # Auto-compute must not be called even though one slot is unset.
        monkeypatch.setattr(
            review,
            "_compute_review_cost_from_window",
            _StubCompute({"cost_dollars": 9.99, "tokens_in": 1, "tokens_out": 2, "model": "x"}),
        )

        args = _ns(skip_cost=True, cost_dollars=0.42, tokens_in=100)
        assert review.cmd_approve(args, db) == 0

        row = _fetch_cost(db)
        assert row["cost_dollars"] == 0.42
        assert row["tokens_in"] == 100
        assert row["tokens_out"] is None


class TestRequestChangesParity:
    def test_request_changes_writes_auto_computed_cost(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(
            review,
            "_compute_review_cost_from_window",
            _StubCompute({"cost_dollars": 0.012, "tokens_in": 99, "tokens_out": 11, "model": "claude-sonnet-4-6"}),
        )

        assert review.cmd_request_changes(_ns(), db) == 0

        row = _fetch_cost(db)
        assert row["status"] == "changes_requested"
        assert row["cost_dollars"] == 0.012
        assert row["tokens_in"] == 99
        assert row["tokens_out"] == 11

    def test_request_changes_leaves_cost_null_when_no_transcript(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(review, "_compute_review_cost_from_window", _StubCompute(None))

        assert review.cmd_request_changes(_ns(), db) == 0

        row = _fetch_cost(db)
        assert row["cost_dollars"] is None
        assert row["tokens_in"] is None
        assert row["tokens_out"] is None


class TestBackwardsCompatibleNamespace:
    """Existing callers without the new cost args must still work."""

    def test_legacy_namespace_without_cost_args_does_not_raise(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(review, "_compute_review_cost_from_window", _StubCompute(None))

        # Mimics the pre-change Namespace shape used by other tests.
        legacy = argparse.Namespace(review_id=1, note=None, model="claude-opus-4-7")
        assert review.cmd_approve(legacy, db) == 0

        row = _fetch_cost(db)
        assert row["status"] == "approved"
        assert row["model"] == "claude-opus-4-7"


class TestResolveCostColumns:
    """Direct unit tests for the resolver helper."""

    def test_returns_explicit_when_all_three_provided(self, monkeypatch):
        sentinel = _StubCompute({"cost_dollars": 9.99, "tokens_in": 1, "tokens_out": 2, "model": "x"})
        monkeypatch.setattr(review, "_compute_review_cost_from_window", sentinel)

        args = _ns(cost_dollars=0.1, tokens_in=10, tokens_out=20)
        result = review._resolve_cost_columns(args, "2026-04-30 12:00:00")

        assert result == (0.1, 10, 20)
        assert sentinel.calls == 0  # no auto-compute when all explicit

    def test_returns_none_tuple_when_no_transcript_and_no_explicit(self, monkeypatch):
        monkeypatch.setattr(review, "_compute_review_cost_from_window", _StubCompute(None))
        result = review._resolve_cost_columns(_ns(), "2026-04-30 12:00:00")
        assert result == (None, None, None)


class TestBackfillCost:
    def test_backfill_populates_null_row(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(
            review,
            "_compute_review_cost_from_window",
            _StubCompute({"cost_dollars": 0.123, "tokens_in": 1000, "tokens_out": 200, "model": "x"}),
        )

        args = argparse.Namespace(review_id=1, force=False)
        assert review.cmd_backfill_cost(args, db) == 0

        row = _fetch_cost(db)
        assert row["cost_dollars"] == 0.123
        assert row["tokens_in"] == 1000
        assert row["tokens_out"] == 200

    def test_backfill_refuses_to_overwrite_without_force(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        # Seed an already-populated row.
        conn = sqlite3.connect(db)
        conn.execute("UPDATE code_reviews SET cost_dollars = 0.5 WHERE id = 1")
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            review,
            "_compute_review_cost_from_window",
            _StubCompute({"cost_dollars": 9.99, "tokens_in": 1, "tokens_out": 2, "model": "x"}),
        )

        args = argparse.Namespace(review_id=1, force=False)
        assert review.cmd_backfill_cost(args, db) == 1

        row = _fetch_cost(db)
        assert row["cost_dollars"] == 0.5  # untouched

    def test_backfill_with_force_overwrites_existing(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE code_reviews SET cost_dollars = 0.5 WHERE id = 1")
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            review,
            "_compute_review_cost_from_window",
            _StubCompute({"cost_dollars": 9.99, "tokens_in": 11, "tokens_out": 22, "model": "x"}),
        )

        args = argparse.Namespace(review_id=1, force=True)
        assert review.cmd_backfill_cost(args, db) == 0

        row = _fetch_cost(db)
        assert row["cost_dollars"] == 9.99
        assert row["tokens_in"] == 11
        assert row["tokens_out"] == 22

    def test_backfill_returns_1_when_no_transcript(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(review, "_compute_review_cost_from_window", _StubCompute(None))

        args = argparse.Namespace(review_id=1, force=False)
        assert review.cmd_backfill_cost(args, db) == 1

        row = _fetch_cost(db)
        assert row["cost_dollars"] is None  # untouched

    def test_backfill_returns_2_when_review_not_found(self, tmp_path):
        db = _make_db(tmp_path)
        args = argparse.Namespace(review_id=999, force=False)
        assert review.cmd_backfill_cost(args, db) == 2

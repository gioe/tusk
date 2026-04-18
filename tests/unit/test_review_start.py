"""Unit tests for tusk-review.py cmd_start: singular-reviewer row creation.

The fan-out reviewer array (review.reviewers) was removed in favor of a single
review.reviewer object. cmd_start always creates exactly one code_reviews row
per pass, taking the reviewer name from (in order):

1. The --reviewer CLI flag (if present)
2. config.review.reviewer.name (if configured)
3. NULL (unassigned), with a stderr warning
"""

import argparse
import importlib.util
import json
import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load tusk-review module (hyphenated filename requires importlib)
_spec = importlib.util.spec_from_file_location(
    "tusk_review",
    os.path.join(REPO_ROOT, "bin", "tusk-review.py"),
)
review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_db(tmp_path):
    """Create a minimal tasks.db with one task and the full code_reviews schema."""
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
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
            agent_name TEXT,
            note TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO tasks (id, summary) VALUES (1, 'sample task');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _make_config(tmp_path, reviewer):
    """Write a minimal review config JSON and return its path.

    `reviewer` may be a dict (the new schema), None, or omitted entirely.
    """
    cfg = {
        "review": {
            "mode": "ai_only",
            "max_passes": 2,
        },
        "review_categories": ["must_fix", "suggest", "defer"],
        "review_severities": ["critical", "major", "minor"],
    }
    if reviewer is not None:
        cfg["review"]["reviewer"] = reviewer
    config_path = str(tmp_path / "config.json")
    with open(config_path, "w") as f:
        json.dump(cfg, f)
    return config_path


def _args(task_id=1, reviewer=None, pass_num=1, diff_summary="test diff", agent=None):
    return argparse.Namespace(
        task_id=task_id,
        reviewer=reviewer,
        pass_num=pass_num,
        diff_summary=diff_summary,
        agent=agent,
    )


def _get_reviews(db_path, task_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, reviewer, status FROM code_reviews WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    conn.close()
    return rows


# ── tests: singular reviewer from config ─────────────────────────────────────


class TestCmdStartConfiguredReviewer:
    def test_configured_reviewer_creates_one_row(self, tmp_path):
        db_path = _make_db(tmp_path)
        config_path = _make_config(
            tmp_path,
            {"name": "general", "description": "General reviewer"},
        )

        rc = review.cmd_start(_args(), db_path, config_path)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 1
        assert rows[0]["reviewer"] == "general"
        assert rows[0]["status"] == "pending"


# ── tests: --reviewer flag overrides config ──────────────────────────────────


class TestCmdStartReviewerOverride:
    def test_reviewer_flag_overrides_config(self, tmp_path):
        """--reviewer <name> creates exactly one row, ignoring the config reviewer."""
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path, {"name": "general", "description": "..."})

        rc = review.cmd_start(_args(reviewer="security"), db_path, config_path)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 1
        assert rows[0]["reviewer"] == "security"


# ── tests: no reviewer configured → unassigned fallback ──────────────────────


class TestCmdStartUnassignedFallback:
    def test_missing_reviewer_creates_one_unassigned_row(self, tmp_path):
        """No review.reviewer key → one pending row with reviewer=NULL."""
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path, None)

        rc = review.cmd_start(_args(), db_path, config_path)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 1
        assert rows[0]["reviewer"] is None
        assert rows[0]["status"] == "pending"

    def test_missing_config_falls_back_to_unassigned(self, tmp_path):
        """Missing/unreadable config → unassigned row."""
        db_path = _make_db(tmp_path)
        nonexistent = str(tmp_path / "does_not_exist.json")

        rc = review.cmd_start(_args(), db_path, nonexistent)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 1
        assert rows[0]["reviewer"] is None


# ── tests: superseded prior pending reviews ──────────────────────────────────


class TestCmdStartSupersedePrior:
    def test_prior_pending_review_is_superseded(self, tmp_path):
        """Starting a new review supersedes any prior pending one for the task."""
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path, {"name": "general", "description": "..."})

        review.cmd_start(_args(), db_path, config_path)
        review.cmd_start(_args(pass_num=2), db_path, config_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, review_pass FROM code_reviews WHERE task_id = 1 ORDER BY id"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0]["status"] == "superseded"
        assert rows[1]["status"] == "pending"
        assert rows[1]["review_pass"] == 2

"""Unit tests for tusk-review.py cmd_start: multi-reviewer row creation.

Regression coverage for issue #390 — 'tusk review start creates only 1 row
regardless of reviewer count in config'.

The loop in cmd_start() has always existed, but there were no tests verifying
that multiple reviewers in config produce multiple code_reviews rows. These
tests document the correct behavior and guard against future regressions.

Root cause analysis: when the config path resolves to config.default.json
(e.g. the user added reviewers to config.default.json instead of
tusk/config.json, or tusk/config.json does not exist) only the default
reviewer set is used. cmd_start now emits a stderr warning in the fallback
case to make such misconfigurations immediately visible.
"""

import argparse
import importlib.util
import json
import os
import sqlite3

import pytest

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


def _make_config(tmp_path, reviewers):
    """Write a minimal review config JSON and return its path."""
    cfg = {
        "review": {
            "mode": "ai_only",
            "max_passes": 2,
            "reviewers": reviewers,
        },
        "review_categories": ["must_fix", "suggest", "defer"],
        "review_severities": ["critical", "major", "minor"],
    }
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


# ── tests: multi-reviewer creation (core regression for issue #390) ──────────


class TestCmdStartMultiReviewer:
    def test_five_reviewers_creates_five_rows(self, tmp_path):
        """Five configured reviewers → five pending rows. Core regression for issue #390."""
        db_path = _make_db(tmp_path)
        config_path = _make_config(
            tmp_path,
            [
                {"name": "general", "description": "General reviewer"},
                {"name": "backend", "description": "Backend reviewer"},
                {"name": "security", "description": "Security reviewer"},
                {"name": "infrastructure", "description": "Infra reviewer"},
                {"name": "docs", "description": "Docs reviewer"},
            ],
        )

        rc = review.cmd_start(_args(), db_path, config_path)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 5
        names = {r["reviewer"] for r in rows}
        assert names == {"general", "backend", "security", "infrastructure", "docs"}
        assert all(r["status"] == "pending" for r in rows)

    def test_two_reviewers_creates_two_rows(self, tmp_path):
        db_path = _make_db(tmp_path)
        config_path = _make_config(
            tmp_path,
            [
                {"name": "general", "description": "General reviewer"},
                {"name": "security", "description": "Security reviewer"},
            ],
        )

        rc = review.cmd_start(_args(), db_path, config_path)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 2
        names = {r["reviewer"] for r in rows}
        assert names == {"general", "security"}

    def test_reviewer_names_extracted_from_dict_items(self, tmp_path):
        """reviewer_item dicts are unwrapped to their 'name' string."""
        db_path = _make_db(tmp_path)
        config_path = _make_config(
            tmp_path,
            [{"name": "alpha", "description": "...", "domains": []},
             {"name": "beta", "description": "...", "domains": ["cli"]}],
        )

        review.cmd_start(_args(), db_path, config_path)

        rows = _get_reviews(db_path)
        names = [r["reviewer"] for r in rows]
        assert "alpha" in names
        assert "beta" in names


# ── tests: single reviewer via --reviewer flag ────────────────────────────────


class TestCmdStartReviewerOverride:
    def test_reviewer_flag_overrides_config(self, tmp_path):
        """--reviewer <name> creates exactly one row, ignoring config reviewers."""
        db_path = _make_db(tmp_path)
        config_path = _make_config(
            tmp_path,
            [{"name": "general"}, {"name": "backend"}],
        )

        rc = review.cmd_start(_args(reviewer="security"), db_path, config_path)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 1
        assert rows[0]["reviewer"] == "security"


# ── tests: no reviewers configured → unassigned fallback ─────────────────────


class TestCmdStartUnassignedFallback:
    def test_empty_reviewers_creates_one_unassigned_row(self, tmp_path):
        """Empty reviewers list → one pending row with reviewer=NULL."""
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path, [])

        rc = review.cmd_start(_args(), db_path, config_path)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 1
        assert rows[0]["reviewer"] is None
        assert rows[0]["status"] == "pending"

    def test_empty_reviewers_emits_warning(self, tmp_path, capsys):
        """When reviewers is empty the user gets a stderr warning (issue #390 diagnostic)."""
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path, [])

        review.cmd_start(_args(), db_path, config_path)

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "no reviewers found" in captured.err
        assert config_path in captured.err

    def test_missing_config_falls_back_to_unassigned_with_warning(self, tmp_path, capsys):
        """Missing/unreadable config → unassigned row + warning (covers wrong-config-path case)."""
        db_path = _make_db(tmp_path)
        nonexistent = str(tmp_path / "does_not_exist.json")

        rc = review.cmd_start(_args(), db_path, nonexistent)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 1
        assert rows[0]["reviewer"] is None
        captured = capsys.readouterr()
        assert "Warning" in captured.err


# ── tests: correct config path is used ───────────────────────────────────────


class TestCmdStartConfigPath:
    def test_uses_provided_config_path_not_default(self, tmp_path):
        """cmd_start reads reviewers from the explicitly passed config_path.

        Regression for issue #390: if resolve_config() returns config.default.json
        (1 reviewer) instead of tusk/config.json (N reviewers), only 1 row is
        created. Verify that the path passed to cmd_start is the one actually used.
        """
        db_path = _make_db(tmp_path)

        # Write two separate configs to distinct filenames
        wrong_dir = tmp_path / "wrong"
        wrong_dir.mkdir()
        _make_config(wrong_dir, [{"name": "general"}])  # simulates config.default.json

        correct_dir = tmp_path / "correct"
        correct_dir.mkdir()
        correct_config = _make_config(correct_dir, [
            {"name": "reviewer_a"},
            {"name": "reviewer_b"},
            {"name": "reviewer_c"},
        ])  # simulates tusk/config.json with 3 custom reviewers

        # Pass the correct config explicitly
        rc = review.cmd_start(_args(), db_path, correct_config)

        assert rc == 0
        rows = _get_reviews(db_path)
        assert len(rows) == 3, (
            f"Expected 3 rows from correct config, got {len(rows)}. "
            "If only 1 row was created, cmd_start may be ignoring the passed config_path."
        )

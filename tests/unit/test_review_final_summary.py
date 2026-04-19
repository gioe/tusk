"""Unit tests for tusk-review-final-summary.py.

Covers (per TASK-116 criterion 513):
- APPROVED verdict rendering (no open must_fix) with a representative comment mix
- CHANGES_REMAINING verdict rendering, including the APPROVED/CHANGES_REMAINING →
  "CHANGES REMAINING" display-label mapping (space, not underscore)
- Deferred-task creation is distinguished from skipped-duplicates via
  review_comments.deferred_task_id (populated = created, NULL = skipped)
- Superseded-review comments still count toward cumulative totals but NOT
  toward the verdict (mirrors tusk-review.py cmd_verdict)

The fixture schema mirrors the columns the helper reads from code_reviews and
review_comments — it is intentionally a minimal subset, not a full mirror of
bin/tusk's canonical CREATE TABLE.
"""

import os
import sqlite3
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


_SCHEMA = """
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
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE review_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    category TEXT,
    severity TEXT,
    comment TEXT NOT NULL,
    resolution TEXT DEFAULT NULL,
    deferred_task_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def _make_db(tmp_path):
    db_path = str(tmp_path / "reviews.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO tasks (id, summary) VALUES (42, 'Ship the thing')")
    conn.commit()
    return db_path, conn


def _run_cli(db_path, review_id, config_path="fake.json"):
    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-review-final-summary.py"),
         db_path, config_path, str(review_id)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


def _insert_review(conn, review_id, task_id=42, status="approved", review_pass=1):
    conn.execute(
        "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
        " VALUES (?, ?, 'alice', ?, ?)",
        (review_id, task_id, status, review_pass),
    )


def _insert_comment(conn, comment_id, review_id, category, resolution=None, deferred_task_id=None):
    conn.execute(
        "INSERT INTO review_comments (id, review_id, category, severity, comment, resolution, deferred_task_id)"
        " VALUES (?, ?, ?, 'minor', ?, ?, ?)",
        (comment_id, review_id, category, f"{category} #{comment_id}", resolution, deferred_task_id),
    )


class TestApprovedVerdict:
    def test_approved_with_mixed_comments_all_resolved(self, tmp_path):
        """Representative mix with zero open must_fix → APPROVED."""
        db_path, conn = _make_db(tmp_path)
        _insert_review(conn, 1, status="approved", review_pass=1)
        # must_fix: 2 found, both fixed
        _insert_comment(conn, 10, 1, "must_fix", resolution="fixed")
        _insert_comment(conn, 11, 1, "must_fix", resolution="fixed")
        # suggest: 3 found, 1 fixed, 1 dismissed, 1 unresolved (must NOT block verdict)
        _insert_comment(conn, 20, 1, "suggest", resolution="fixed")
        _insert_comment(conn, 21, 1, "suggest", resolution="dismissed")
        _insert_comment(conn, 22, 1, "suggest", resolution=None)
        # defer: 3 found; 2 created tasks (deferred_task_id set), 1 skipped (NULL)
        _insert_comment(conn, 30, 1, "defer", resolution="deferred", deferred_task_id=101)
        _insert_comment(conn, 31, 1, "defer", resolution="deferred", deferred_task_id=102)
        _insert_comment(conn, 32, 1, "defer", resolution="deferred", deferred_task_id=None)
        conn.commit()

        code, out, err = _run_cli(db_path, 1)
        assert code == 0, err
        assert "Review complete for Task 42: Ship the thing" in out
        assert "Pass:      1" in out
        assert "must_fix:  2 found, 2 fixed" in out
        assert "suggest:   3 found, 1 fixed, 1 dismissed" in out
        assert "defer:     3 found, 2 tasks created, 1 skipped (duplicate)" in out
        assert "Verdict: APPROVED" in out
        # The machine-ID form must not leak into the display label
        assert "CHANGES_REMAINING" not in out

    def test_approved_with_no_comments(self, tmp_path):
        """No findings at all still renders the full block with zero counts."""
        db_path, conn = _make_db(tmp_path)
        _insert_review(conn, 7, status="approved", review_pass=2)
        conn.commit()

        code, out, err = _run_cli(db_path, 7)
        assert code == 0, err
        assert "Pass:      2" in out
        assert "must_fix:  0 found, 0 fixed" in out
        assert "suggest:   0 found, 0 fixed, 0 dismissed" in out
        assert "defer:     0 found, 0 tasks created, 0 skipped (duplicate)" in out
        assert "Verdict: APPROVED" in out


class TestChangesRemainingVerdict:
    def test_changes_remaining_maps_underscore_to_space(self, tmp_path):
        """Open must_fix → machine verdict CHANGES_REMAINING → display 'CHANGES REMAINING'."""
        db_path, conn = _make_db(tmp_path)
        _insert_review(conn, 1, status="changes_requested", review_pass=1)
        # One unresolved must_fix is enough to flip the verdict
        _insert_comment(conn, 10, 1, "must_fix", resolution=None)
        _insert_comment(conn, 11, 1, "must_fix", resolution="fixed")
        _insert_comment(conn, 20, 1, "suggest", resolution="dismissed")
        _insert_comment(conn, 30, 1, "defer", resolution="deferred", deferred_task_id=None)
        conn.commit()

        code, out, err = _run_cli(db_path, 1)
        assert code == 0, err
        assert "must_fix:  2 found, 1 fixed" in out
        assert "suggest:   1 found, 0 fixed, 1 dismissed" in out
        assert "defer:     1 found, 0 tasks created, 1 skipped (duplicate)" in out
        assert "Verdict: CHANGES REMAINING" in out
        # The raw machine form must not appear in output
        assert "CHANGES_REMAINING" not in out

    def test_superseded_must_fix_excluded_from_verdict_but_counted_in_totals(self, tmp_path):
        """Multi-pass case: pass-1 comments (superseded) count in cumulative totals
        but do NOT block the verdict when pass-2 has no open must_fix.
        This is what makes the final-summary block trustworthy across passes."""
        db_path, conn = _make_db(tmp_path)
        # Pass 1: superseded, had one must_fix that was fixed
        _insert_review(conn, 1, status="superseded", review_pass=1)
        _insert_comment(conn, 10, 1, "must_fix", resolution="fixed")
        # Pass 2: approved, no open findings
        _insert_review(conn, 2, status="approved", review_pass=2)
        conn.commit()

        # Final summary requested for the pass-2 review
        code, out, err = _run_cli(db_path, 2)
        assert code == 0, err
        assert "Pass:      2" in out
        # Cumulative must_fix total includes the superseded row
        assert "must_fix:  1 found, 1 fixed" in out
        # But verdict remains APPROVED because no unresolved must_fix on
        # non-superseded reviews
        assert "Verdict: APPROVED" in out

    def test_superseded_open_must_fix_still_excluded_from_verdict(self, tmp_path):
        """A stale unresolved must_fix on a superseded review must NOT flip the
        verdict to CHANGES REMAINING — the verdict only considers non-superseded
        reviews, matching `tusk review verdict`."""
        db_path, conn = _make_db(tmp_path)
        _insert_review(conn, 1, status="superseded", review_pass=1)
        _insert_comment(conn, 10, 1, "must_fix", resolution=None)  # stale, but on superseded
        _insert_review(conn, 2, status="approved", review_pass=2)
        conn.commit()

        code, out, err = _run_cli(db_path, 2)
        assert code == 0, err
        assert "Verdict: APPROVED" in out


class TestErrorPaths:
    def test_missing_review_returns_exit_2(self, tmp_path):
        db_path, _ = _make_db(tmp_path)
        code, out, err = _run_cli(db_path, 999)
        assert code == 2
        assert "Review 999 not found" in err

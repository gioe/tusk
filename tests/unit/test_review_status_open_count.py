"""Regression test: tusk review status open count with no comments.

Before the fix, cmd_status used LEFT JOIN with SUM(CASE WHEN c.resolution IS NULL ...)
which counted phantom NULL rows from the join as open comments, reporting open: 1
even when a review had no comments at all.
"""

import sqlite3


def _make_db():
    """Create an in-memory DB with the minimal schema needed by cmd_status."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT
        );
        CREATE TABLE code_reviews (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            reviewer TEXT,
            status TEXT,
            review_pass INTEGER,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE review_comments (
            id INTEGER PRIMARY KEY,
            review_id INTEGER,
            file_path TEXT,
            line_start INTEGER,
            category TEXT,
            severity TEXT,
            comment TEXT,
            resolution TEXT
        );
        """
    )
    return conn


STATUS_QUERY = (
    "SELECT r.id, r.reviewer, r.status, r.review_pass, r.created_at, r.updated_at,"
    "  COUNT(c.id) as total_comments,"
    "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution IS NULL THEN 1 ELSE 0 END) as open_comments,"
    "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution = 'fixed' THEN 1 ELSE 0 END) as fixed_comments,"
    "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution = 'dismissed' THEN 1 ELSE 0 END) as dismissed_comments"
    " FROM code_reviews r"
    " LEFT JOIN review_comments c ON c.review_id = r.id"
    " WHERE r.task_id = ?"
    " GROUP BY r.id ORDER BY r.id"
)


class TestReviewStatusOpenCount:
    def test_approved_with_no_comments_reports_zero_open(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks VALUES (1, 'test task')")
        conn.execute(
            "INSERT INTO code_reviews VALUES (1, 1, 'reviewer-a', 'approved', 1, '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        rows = conn.execute(STATUS_QUERY, (1,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["total_comments"] == 0
        assert rows[0]["open_comments"] == 0
        conn.close()

    def test_review_with_open_comment_reports_correct_count(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks VALUES (1, 'test task')")
        conn.execute(
            "INSERT INTO code_reviews VALUES (1, 1, 'reviewer-a', 'changes_requested', 0, '2026-01-01', '2026-01-01')"
        )
        # One open comment, one resolved
        conn.execute(
            "INSERT INTO review_comments VALUES (1, 1, 'foo.py', 10, 'must_fix', 'major', 'broken', NULL)"
        )
        conn.execute(
            "INSERT INTO review_comments VALUES (2, 1, 'bar.py', 5, 'suggest', 'minor', 'style', 'dismissed')"
        )
        conn.commit()

        rows = conn.execute(STATUS_QUERY, (1,)).fetchall()
        assert rows[0]["total_comments"] == 2
        assert rows[0]["open_comments"] == 1
        assert rows[0]["dismissed_comments"] == 1
        conn.close()

    def test_all_comments_resolved_reports_zero_open(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks VALUES (1, 'test task')")
        conn.execute(
            "INSERT INTO code_reviews VALUES (1, 1, 'reviewer-a', 'approved', 1, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO review_comments VALUES (1, 1, 'foo.py', 10, 'must_fix', 'major', 'broken', 'fixed')"
        )
        conn.commit()

        rows = conn.execute(STATUS_QUERY, (1,)).fetchall()
        assert rows[0]["total_comments"] == 1
        assert rows[0]["open_comments"] == 0
        assert rows[0]["fixed_comments"] == 1
        conn.close()

    def test_multiple_reviews_zero_comments_all_report_zero_open(self):
        """Multiple approved reviews with no comments should all report open: 0."""
        conn = _make_db()
        conn.execute("INSERT INTO tasks VALUES (1, 'test task')")
        conn.execute(
            "INSERT INTO code_reviews VALUES (1, 1, 'reviewer-a', 'approved', 1, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO code_reviews VALUES (2, 1, 'reviewer-b', 'approved', 1, '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        rows = conn.execute(STATUS_QUERY, (1,)).fetchall()
        assert len(rows) == 2
        assert all(r["open_comments"] == 0 for r in rows)
        conn.close()

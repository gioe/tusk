"""Unit tests for tusk-review.py subcommands: cmd_list, cmd_approve, cmd_summary.

These tests exercise the core SQL query paths using in-memory SQLite,
matching the pattern in test_review_status_open_count.py.
"""

import sqlite3


def _make_db():
    """Create an in-memory DB with the schema columns used by cmd_list/approve/summary."""
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
            diff_summary TEXT,
            note TEXT,
            created_at TEXT DEFAULT '2026-01-01',
            updated_at TEXT DEFAULT '2026-01-01'
        );
        CREATE TABLE review_comments (
            id INTEGER PRIMARY KEY,
            review_id INTEGER,
            file_path TEXT,
            line_start INTEGER,
            line_end INTEGER,
            category TEXT,
            severity TEXT,
            comment TEXT,
            resolution TEXT
        );
        """
    )
    return conn


# ─── cmd_list queries ────────────────────────────────────────────────────────

# Mirrors the queries in cmd_list()
_REVIEWS_QUERY = (
    "SELECT id, reviewer, status, review_pass, created_at"
    " FROM code_reviews WHERE task_id = ? ORDER BY id"
)
_COMMENTS_QUERY_TMPL = (
    "SELECT id, review_id, file_path, line_start, category, severity, comment, resolution"
    " FROM review_comments WHERE review_id IN ({ph}) ORDER BY review_id, category, id"
)


class TestCmdList:
    def test_task_with_no_reviews(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'my task')")
        conn.commit()

        reviews = conn.execute(_REVIEWS_QUERY, (1,)).fetchall()
        assert reviews == []
        conn.close()

    def test_review_with_no_comments(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'my task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            " VALUES (1, 1, 'alice', 'pending', 1)"
        )
        conn.commit()

        reviews = conn.execute(_REVIEWS_QUERY, (1,)).fetchall()
        assert len(reviews) == 1
        assert reviews[0]["reviewer"] == "alice"

        review_ids = [r["id"] for r in reviews]
        ph = ",".join("?" * len(review_ids))
        comments = conn.execute(_COMMENTS_QUERY_TMPL.format(ph=ph), review_ids).fetchall()
        assert comments == []
        conn.close()

    def test_review_with_comments_ordered_by_category(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'my task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            " VALUES (1, 1, 'alice', 'changes_requested', 0)"
        )
        conn.execute(
            "INSERT INTO review_comments (id, review_id, file_path, line_start, category, severity, comment, resolution)"
            " VALUES (1, 1, 'foo.py', 10, 'suggest', 'minor', 'rename var', NULL)"
        )
        conn.execute(
            "INSERT INTO review_comments (id, review_id, file_path, line_start, category, severity, comment, resolution)"
            " VALUES (2, 1, 'bar.py', 5, 'must_fix', 'critical', 'null pointer', NULL)"
        )
        conn.commit()

        review_ids = [1]
        ph = ",".join("?" * len(review_ids))
        comments = conn.execute(_COMMENTS_QUERY_TMPL.format(ph=ph), review_ids).fetchall()
        assert len(comments) == 2
        # ordered by category, so must_fix before suggest
        assert comments[0]["category"] == "must_fix"
        assert comments[1]["category"] == "suggest"
        conn.close()

    def test_multiple_reviews_comments_bucketed_per_review(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'my task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            " VALUES (1, 1, 'alice', 'approved', 1)"
        )
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            " VALUES (2, 1, 'bob', 'changes_requested', 0)"
        )
        # Only review 2 has a comment
        conn.execute(
            "INSERT INTO review_comments (id, review_id, file_path, line_start, category, severity, comment, resolution)"
            " VALUES (1, 2, 'foo.py', 3, 'must_fix', 'critical', 'bug', NULL)"
        )
        conn.commit()

        reviews = conn.execute(_REVIEWS_QUERY, (1,)).fetchall()
        assert len(reviews) == 2

        review_ids = [r["id"] for r in reviews]
        ph = ",".join("?" * len(review_ids))
        buckets: dict[int, list] = {rid: [] for rid in review_ids}
        for c in conn.execute(_COMMENTS_QUERY_TMPL.format(ph=ph), review_ids).fetchall():
            buckets[c["review_id"]].append(c)

        assert buckets[1] == []
        assert len(buckets[2]) == 1
        assert buckets[2][0]["comment"] == "bug"
        conn.close()


# ─── cmd_approve queries ─────────────────────────────────────────────────────

_APPROVE_NO_NOTE = (
    "UPDATE code_reviews SET status = 'approved', review_pass = 1,"
    " updated_at = datetime('now') WHERE id = ?"
)
_APPROVE_WITH_NOTE = (
    "UPDATE code_reviews SET status = 'approved', review_pass = 1,"
    " note = ?, updated_at = datetime('now') WHERE id = ?"
)


class TestCmdApprove:
    def _db_with_review(self, status="pending"):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            f" VALUES (1, 1, 'alice', '{status}', 1)"
        )
        conn.commit()
        return conn

    def test_approve_sets_status_and_pass(self):
        conn = self._db_with_review()
        conn.execute(_APPROVE_NO_NOTE, (1,))
        conn.commit()

        row = conn.execute("SELECT status, review_pass FROM code_reviews WHERE id = 1").fetchone()
        assert row["status"] == "approved"
        assert row["review_pass"] == 1
        conn.close()

    def test_approve_with_note_persists_note(self):
        conn = self._db_with_review()
        conn.execute(_APPROVE_WITH_NOTE, ("LGTM", 1))
        conn.commit()

        row = conn.execute("SELECT status, note FROM code_reviews WHERE id = 1").fetchone()
        assert row["status"] == "approved"
        assert row["note"] == "LGTM"
        conn.close()

    def test_approve_already_approved_review_is_idempotent(self):
        conn = self._db_with_review(status="approved")
        conn.execute(_APPROVE_NO_NOTE, (1,))
        conn.commit()

        row = conn.execute("SELECT status FROM code_reviews WHERE id = 1").fetchone()
        assert row["status"] == "approved"
        conn.close()

    def test_approve_nonexistent_review_leaves_db_unchanged(self):
        conn = self._db_with_review()
        conn.execute(_APPROVE_NO_NOTE, (999,))
        conn.commit()

        row = conn.execute("SELECT status FROM code_reviews WHERE id = 1").fetchone()
        assert row["status"] == "pending"  # unchanged
        conn.close()


# ─── cmd_summary queries ─────────────────────────────────────────────────────

_SUMMARY_REVIEW_QUERY = (
    "SELECT r.id, r.task_id, r.reviewer, r.status, r.review_pass,"
    "  r.diff_summary, r.created_at, t.summary as task_summary"
    " FROM code_reviews r JOIN tasks t ON t.id = r.task_id"
    " WHERE r.id = ?"
)
_SUMMARY_COMMENTS_QUERY = (
    "SELECT id, file_path, line_start, line_end, category, severity, comment, resolution"
    " FROM review_comments WHERE review_id = ? ORDER BY severity, category, id"
)


class TestCmdSummary:
    def test_review_join_returns_task_summary(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'important feature')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass, diff_summary)"
            " VALUES (1, 1, 'bob', 'approved', 1, 'small diff')"
        )
        conn.commit()

        row = conn.execute(_SUMMARY_REVIEW_QUERY, (1,)).fetchone()
        assert row is not None
        assert row["task_summary"] == "important feature"
        assert row["reviewer"] == "bob"
        assert row["status"] == "approved"
        assert row["diff_summary"] == "small diff"
        conn.close()

    def test_nonexistent_review_returns_none(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'task')")
        conn.commit()

        row = conn.execute(_SUMMARY_REVIEW_QUERY, (999,)).fetchone()
        assert row is None
        conn.close()

    def test_comments_ordered_by_severity_then_category(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            " VALUES (1, 1, 'alice', 'changes_requested', 0)"
        )
        conn.execute(
            "INSERT INTO review_comments (id, review_id, file_path, line_start, category, severity, comment, resolution)"
            " VALUES (1, 1, 'a.py', 1, 'must_fix', 'critical', 'null pointer', NULL)"
        )
        conn.execute(
            "INSERT INTO review_comments (id, review_id, file_path, line_start, category, severity, comment, resolution)"
            " VALUES (2, 1, 'b.py', 2, 'suggest', 'minor', 'style nit', 'dismissed')"
        )
        conn.commit()

        comments = conn.execute(_SUMMARY_COMMENTS_QUERY, (1,)).fetchall()
        assert len(comments) == 2
        open_comments = [c for c in comments if c["resolution"] is None]
        resolved_comments = [c for c in comments if c["resolution"] is not None]
        assert len(open_comments) == 1
        assert len(resolved_comments) == 1
        assert open_comments[0]["comment"] == "null pointer"
        assert resolved_comments[0]["resolution"] == "dismissed"
        conn.close()

    def test_no_comments_returns_empty(self):
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            " VALUES (1, 1, 'alice', 'approved', 1)"
        )
        conn.commit()

        comments = conn.execute(_SUMMARY_COMMENTS_QUERY, (1,)).fetchall()
        assert comments == []
        conn.close()

    def test_line_end_included_in_comment_row(self):
        """Verify line_end is selected — cmd_summary uses it for location display."""
        conn = _make_db()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            " VALUES (1, 1, 'alice', 'changes_requested', 0)"
        )
        conn.execute(
            "INSERT INTO review_comments (id, review_id, file_path, line_start, line_end, category, severity, comment, resolution)"
            " VALUES (1, 1, 'main.py', 10, 20, 'must_fix', 'critical', 'large block broken', NULL)"
        )
        conn.commit()

        comments = conn.execute(_SUMMARY_COMMENTS_QUERY, (1,)).fetchall()
        assert len(comments) == 1
        assert comments[0]["line_end"] == 20
        conn.close()

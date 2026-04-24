"""Integration tests for the stale-progress warning in tusk task-start.

When a task's summary is materially rewritten after prior progress checkpoints
were logged, those checkpoints' next_steps describe obsolete work and mislead
future /tusk sessions. tusk task-start computes a vocabulary-overlap ratio
between the latest progress.next_steps and the current summary/description; a
ratio below _STALE_PROGRESS_OVERLAP_THRESHOLD triggers a stderr warning that
includes the entry's created_at so the operator can judge staleness.

These tests lock three behaviors:
  - Scope rewrite (old next_steps references unrelated work) → warning fires,
    includes the created_at timestamp of the stale entry
  - Small edit (typo fix that preserves vocabulary) → no warning
  - Helper: short next_steps (< _STALE_PROGRESS_MIN_TOKENS) stays quiet
"""

import importlib.util
import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_task_start",
    os.path.join(REPO_ROOT, "bin", "tusk-task-start.py"),
)
tusk_task_start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_task_start)


def _insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    description: str = "",
    status: str = "To Do",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, description, status, priority, complexity,
                           task_type, priority_score)
        VALUES (?, ?, ?, 'Medium', 'S', 'feature', 50)
        """,
        (summary, description, status),
    )
    conn.commit()
    return cur.lastrowid


def _insert_criterion(conn: sqlite3.Connection, task_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed)"
        " VALUES (?, 'do the thing', 'original', 0)",
        (task_id,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_progress(conn: sqlite3.Connection, task_id: int, next_steps: str,
                     *, created_at: str | None = None) -> int:
    if created_at is None:
        cur = conn.execute(
            "INSERT INTO task_progress (task_id, next_steps) VALUES (?, ?)",
            (task_id, next_steps),
        )
    else:
        cur = conn.execute(
            "INSERT INTO task_progress (task_id, next_steps, created_at)"
            " VALUES (?, ?, ?)",
            (task_id, next_steps, created_at),
        )
    conn.commit()
    return cur.lastrowid


def _call_start(db_path, config_path, *extra_args):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_start.main([str(db_path), str(config_path), *extra_args])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


class TestStaleProgressWarning:
    def test_scope_rewrite_fires_warning_with_created_at(self, db_path, config_path):
        """Material summary divergence → warning fires, names the created_at
        timestamp so the operator can judge how stale the handoff is."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = _insert_task(
                conn,
                summary="Migrate database to PostgreSQL from SQLite",
                description="Replace the SQLite backend with PostgreSQL everywhere.",
            )
            _insert_criterion(conn, task_id)
            _insert_progress(
                conn,
                task_id,
                "Implemented caching wrapper for /foo and /bar endpoints. Need invalidation handler and integration tests.",
                created_at="2026-04-01 12:34:56",
            )
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result is not None
        assert "prior progress for task" in stderr
        assert "may be stale" in stderr
        assert "2026-04-01 12:34:56" in stderr
        assert "next_steps:" in stderr

    def test_small_summary_edit_stays_quiet(self, db_path, config_path):
        """Typo fix / minor rewording → summary still shares most vocabulary
        with next_steps, so the warning does NOT fire."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # Summary reworded ("typo" → "typos"), same scope as the progress note.
            task_id = _insert_task(
                conn,
                summary="Fix typos in login flow",
                description="Correct the typos in the login flow validation code.",
            )
            _insert_criterion(conn, task_id)
            _insert_progress(
                conn,
                task_id,
                "Reviewed login flow validation, identified the typo in flow.py line 42. Need to update corresponding tests.",
                created_at="2026-04-01 12:34:56",
            )
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result is not None
        assert "may be stale" not in stderr

    def test_in_progress_checkpoint_same_scope_stays_quiet(self, db_path, config_path):
        """A typical mid-task checkpoint whose next_steps references the current
        feature vocabulary must not fire the warning — this is the common
        resume-work path and MUST remain silent."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = _insert_task(
                conn,
                summary="Add caching to API endpoints",
                description="Add an LRU cache layer in front of the API endpoint handlers.",
            )
            _insert_criterion(conn, task_id)
            _insert_progress(
                conn,
                task_id,
                "Implemented cache wrapper in api.py. Need to add invalidation handler for /foo and /bar endpoints, then write tests.",
                created_at="2026-04-01 12:34:56",
            )
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result is not None
        assert "may be stale" not in stderr

    def test_no_progress_no_warning(self, db_path, config_path):
        """Task without any progress rows must not produce a stale-progress
        warning (guard against false positives on fresh tasks)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = _insert_task(
                conn,
                summary="Completely unrelated new task",
                description="No prior progress to compare against.",
            )
            _insert_criterion(conn, task_id)
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result is not None
        assert "may be stale" not in stderr

    def test_only_most_recent_progress_row_evaluated(self, db_path, config_path):
        """Only the newest non-empty next_steps is judged. A stale older entry
        sitting behind a fresh newer entry must NOT trigger the warning —
        the newer entry supersedes it by design."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = _insert_task(
                conn,
                summary="Add caching to API endpoints",
                description="Add an LRU cache layer in front of the API endpoint handlers.",
            )
            _insert_criterion(conn, task_id)
            # Older stale entry from a prior scope.
            _insert_progress(
                conn,
                task_id,
                "Migrated database to PostgreSQL and swapped out the SQLite driver.",
                created_at="2026-03-01 09:00:00",
            )
            # Newer entry aligned with current summary — should make warning quiet.
            _insert_progress(
                conn,
                task_id,
                "Implemented cache wrapper in api.py. Need invalidation handler for /foo and /bar endpoints.",
                created_at="2026-04-15 09:00:00",
            )
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result is not None
        assert "may be stale" not in stderr


class TestStaleProgressHelpers:
    def test_short_next_steps_is_not_flagged(self):
        """next_steps shorter than _STALE_PROGRESS_MIN_TOKENS must return False
        — too short to judge divergence reliably."""
        assert tusk_task_start._progress_next_steps_is_stale(
            "WIP.", "Completely unrelated current summary and description"
        ) is False

    def test_empty_current_text_is_not_flagged(self):
        """No current vocabulary → can't compute a meaningful overlap → quiet."""
        assert tusk_task_start._progress_next_steps_is_stale(
            "Implemented caching wrapper for /foo and /bar endpoints. Need invalidation handler and integration tests.",
            "",
        ) is False

    def test_scope_rewrite_flagged(self):
        """Helper directly: scope rewrite crosses the threshold."""
        assert tusk_task_start._progress_next_steps_is_stale(
            "Implemented caching wrapper for /foo and /bar endpoints. Need invalidation handler and integration tests.",
            "Migrate database to PostgreSQL from SQLite",
        ) is True

    def test_stem_collapses_common_suffixes(self):
        """Stem-based tokenization collapses caching/cache and typo/typos so
        small edits (pluralization, gerund form) stay quiet."""
        tokens = tusk_task_start._extract_content_tokens(
            "caching cache typo typos implementing implemented"
        )
        assert "cach" in tokens
        assert "typo" in tokens
        assert "implement" in tokens

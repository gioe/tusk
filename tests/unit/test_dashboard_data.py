"""Unit tests for tusk-dashboard-data.py data-layer computations.

Imports tusk-dashboard-data.py via importlib (hyphenated filename requires it).
Builds an in-memory SQLite DB with the minimal tusk schema and seeds controlled
data to assert computed values without touching the real tusk database.
"""

import importlib.util
import os
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Load module under test
# ---------------------------------------------------------------------------

def _load_dashboard_data():
    path = os.path.join(REPO_ROOT, "bin", "tusk-dashboard-data.py")
    spec = importlib.util.spec_from_file_location("tusk_dashboard_data", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dashboard_data = _load_dashboard_data()


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'To Do',
    priority TEXT DEFAULT 'Medium',
    domain TEXT,
    assignee TEXT,
    task_type TEXT,
    priority_score INTEGER DEFAULT 0,
    expires_at TEXT,
    closed_reason TEXT,
    complexity TEXT,
    is_deferred INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    closed_at TEXT
);

CREATE TABLE task_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER,
    cost_dollars REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    model TEXT,
    agent_name TEXT,
    peak_context_tokens INTEGER,
    first_context_tokens INTEGER,
    last_context_tokens INTEGER,
    context_window INTEGER,
    request_count INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE VIEW task_metrics AS
SELECT t.*,
    COUNT(s.id) as session_count,
    SUM(s.duration_seconds) as total_duration_seconds,
    SUM(s.cost_dollars) as total_cost,
    SUM(s.tokens_in) as total_tokens_in,
    SUM(s.tokens_out) as total_tokens_out,
    SUM(s.lines_added) as total_lines_added,
    SUM(s.lines_removed) as total_lines_removed,
    SUM(s.request_count) as total_request_count
FROM tasks t
LEFT JOIN task_sessions s ON t.id = s.task_id
GROUP BY t.id;
"""


def _make_conn():
    """Create an in-memory SQLite connection with the minimal tusk schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------


class TestModuleLoading:
    def test_module_loads_successfully(self):
        """tusk-dashboard-data.py can be imported via importlib."""
        mod = _load_dashboard_data()
        assert mod is not None

    def test_fetch_functions_are_callable(self):
        mod = _load_dashboard_data()
        assert callable(mod.fetch_task_metrics)
        assert callable(mod.fetch_kpi_data)
        assert callable(mod.fetch_complexity_metrics)


# ---------------------------------------------------------------------------
# duration_in_status_seconds: To Do branch
# ---------------------------------------------------------------------------


class TestDurationToDoStatus:
    def test_todo_task_duration_is_positive(self):
        """To Do tasks: duration = now - created_at (always > 0 for past creation)."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (summary, status, created_at) VALUES (?, ?, ?)",
            ("todo task", "To Do", "2026-01-01 00:00:00"),
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        assert len(rows) == 1
        assert rows[0]["duration_in_status_seconds"] > 0

    def test_todo_task_duration_uses_created_at(self):
        """To Do: duration grows with older created_at, not started_at."""
        conn = _make_conn()
        # Two tasks with different creation times
        conn.execute(
            "INSERT INTO tasks (summary, status, created_at) VALUES (?, ?, ?)",
            ("older", "To Do", "2025-01-01 00:00:00"),
        )
        conn.execute(
            "INSERT INTO tasks (summary, status, created_at) VALUES (?, ?, ?)",
            ("newer", "To Do", "2026-01-01 00:00:00"),
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        durations = {r["summary"]: r["duration_in_status_seconds"] for r in rows}
        assert durations["older"] > durations["newer"]


# ---------------------------------------------------------------------------
# duration_in_status_seconds: In Progress branch (with sessions)
# ---------------------------------------------------------------------------


class TestDurationInProgressStatus:
    def test_in_progress_with_session_uses_min_started_at(self):
        """In Progress + sessions: duration = now - MIN(session.started_at)."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, created_at, started_at) VALUES (?, ?, ?, ?, ?)",
            (1, "wip task", "In Progress", "2026-01-01 00:00:00", "2026-01-15 00:00:00"),
        )
        # Two sessions with different started_at; MIN should win
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) VALUES (?, ?, ?, ?)",
            (1, "2026-01-10 00:00:00", "2026-01-10 01:00:00", 3600),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) VALUES (?, ?, ?, ?)",
            (1, "2026-01-20 00:00:00", None, None),
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        assert len(rows) == 1
        duration = rows[0]["duration_in_status_seconds"]
        # MIN session started_at is 2026-01-10; that's earlier than started_at=2026-01-15.
        # Duration should be positive and reflect the earlier session start.
        assert duration > 0
        # Verify it's anchored to session start (2026-01-10), not task.started_at (2026-01-15).
        # Duration from 2026-01-10 must exceed duration from 2026-01-15.
        days_since_session_start = (
            conn.execute(
                "SELECT CAST((julianday('now') - julianday('2026-01-10 00:00:00')) * 86400 AS INTEGER)"
            ).fetchone()[0]
        )
        days_since_task_start = (
            conn.execute(
                "SELECT CAST((julianday('now') - julianday('2026-01-15 00:00:00')) * 86400 AS INTEGER)"
            ).fetchone()[0]
        )
        assert duration >= days_since_session_start - 5  # allow ±5 s rounding
        assert duration > days_since_task_start


# ---------------------------------------------------------------------------
# duration_in_status_seconds: Done branch (with sessions)
# ---------------------------------------------------------------------------


class TestDurationDoneStatus:
    def test_done_with_sessions_uses_max_ended_at_minus_min_started_at(self):
        """Done + sessions: duration = MAX(ended_at) - MIN(started_at)."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason) VALUES (?, ?, ?, ?)",
            (1, "done task", "Done", "completed"),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) VALUES (?, ?, ?, ?)",
            (1, "2026-01-01 00:00:00", "2026-01-01 02:00:00", 7200),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) VALUES (?, ?, ?, ?)",
            (1, "2026-01-02 00:00:00", "2026-01-02 01:00:00", 3600),
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        assert len(rows) == 1
        duration = rows[0]["duration_in_status_seconds"]
        # MAX ended_at = 2026-01-02 01:00:00, MIN started_at = 2026-01-01 00:00:00 => 25 hours = 90000 s
        expected = 90000
        assert abs(duration - expected) < 5  # allow ±5 s for rounding


# ---------------------------------------------------------------------------
# duration_in_status_seconds: In Progress fallback (no sessions)
# ---------------------------------------------------------------------------


class TestDurationInProgressFallback:
    def test_in_progress_no_sessions_uses_started_at_not_created_at(self):
        """In Progress + no sessions: fallback is task.started_at, not created_at."""
        conn = _make_conn()
        # created_at is much earlier than started_at
        conn.execute(
            "INSERT INTO tasks (id, summary, status, created_at, started_at) VALUES (?, ?, ?, ?, ?)",
            (1, "wip no session", "In Progress",
             "2026-01-01 00:00:00",   # created_at (older)
             "2026-02-01 00:00:00"),  # started_at (newer)
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        assert len(rows) == 1
        duration = rows[0]["duration_in_status_seconds"]

        # duration from started_at (2026-02-01) should be smaller than from created_at (2026-01-01)
        duration_from_created = conn.execute(
            "SELECT CAST((julianday('now') - julianday('2026-01-01 00:00:00')) * 86400 AS INTEGER)"
        ).fetchone()[0]
        duration_from_started = conn.execute(
            "SELECT CAST((julianday('now') - julianday('2026-02-01 00:00:00')) * 86400 AS INTEGER)"
        ).fetchone()[0]

        assert abs(duration - duration_from_started) < 5
        assert duration < duration_from_created


# ---------------------------------------------------------------------------
# duration_in_status_seconds: Done fallback (no sessions)
# ---------------------------------------------------------------------------


class TestDurationDoneFallback:
    def test_done_no_sessions_uses_updated_at_minus_started_at(self):
        """Done + no sessions: duration = updated_at - started_at."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, created_at, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                1, "done no session", "Done", "completed",
                "2026-01-01 00:00:00",  # created_at
                "2026-01-02 00:00:00",  # started_at
                "2026-01-02 03:00:00",  # updated_at  => span = 3 hours = 10800 s
            ),
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        assert len(rows) == 1
        duration = rows[0]["duration_in_status_seconds"]
        # updated_at - started_at = 3 hours = 10800 s
        assert abs(duration - 10800) < 5

    def test_done_no_sessions_no_started_at_falls_back_to_created_at(self):
        """Done + no sessions + no started_at: start anchor falls back to created_at."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                1, "done fallback", "Done", "completed",
                "2026-01-01 00:00:00",  # created_at (used as start fallback)
                "2026-01-01 02:00:00",  # updated_at  => span = 2 hours = 7200 s
            ),
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        assert len(rows) == 1
        assert abs(rows[0]["duration_in_status_seconds"] - 7200) < 5


# ---------------------------------------------------------------------------
# total_duration_seconds aggregation
# ---------------------------------------------------------------------------


class TestTotalDurationSeconds:
    def test_total_duration_sums_session_duration_seconds(self):
        """total_duration_seconds = SUM of session.duration_seconds."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task with sessions")
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) VALUES (?, ?, ?, ?)",
            (1, "2026-01-01 00:00:00", "2026-01-01 01:00:00", 3600),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) VALUES (?, ?, ?, ?)",
            (1, "2026-01-02 00:00:00", "2026-01-02 00:30:00", 1800),
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        assert len(rows) == 1
        assert rows[0]["total_duration_seconds"] == 5400  # 3600 + 1800

    def test_total_duration_zero_when_no_sessions(self):
        """total_duration_seconds COALESCE'd to 0 when task has no sessions."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "no sessions"))
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        assert len(rows) == 1
        assert rows[0]["total_duration_seconds"] == 0

    def test_total_duration_across_multiple_tasks(self):
        """total_duration_seconds is per-task, not a cross-task sum."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task A"))
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (2, "task B"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, duration_seconds) VALUES (?, ?, ?)",
            (1, "2026-01-01 00:00:00", 1000),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, duration_seconds) VALUES (?, ?, ?)",
            (2, "2026-01-01 00:00:00", 2000),
        )
        conn.commit()

        rows = dashboard_data.fetch_task_metrics(conn)
        by_id = {r["id"]: r for r in rows}
        assert by_id[1]["total_duration_seconds"] == 1000
        assert by_id[2]["total_duration_seconds"] == 2000


# ---------------------------------------------------------------------------
# fetch_kpi_data()
# ---------------------------------------------------------------------------


class TestFetchKpiData:
    def test_totals_match_seeded_sessions(self):
        """fetch_kpi_data() aggregates cost and tokens across all sessions."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "t1"))
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (2, "t2"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-01-01 00:00:00", 0.10, 1000, 500),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-01-02 00:00:00", 0.20, 2000, 1000),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?)",
            (2, "2026-01-03 00:00:00", 0.05, 500, 250),
        )
        conn.commit()

        kpi = dashboard_data.fetch_kpi_data(conn)

        assert abs(kpi["total_cost"] - 0.35) < 1e-9
        assert kpi["total_tokens_in"] == 3500
        assert kpi["total_tokens_out"] == 1750
        assert kpi["total_tokens"] == 5250

    def test_tasks_completed_counts_done_tasks(self):
        """fetch_kpi_data() counts only Done tasks as completed."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason) VALUES (?, ?, ?, ?)",
            (1, "done1", "Done", "completed"),
        )
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason) VALUES (?, ?, ?, ?)",
            (2, "done2", "Done", "completed"),
        )
        conn.execute("INSERT INTO tasks (id, summary, status) VALUES (?, ?, ?)", (3, "wip", "In Progress"))
        conn.commit()

        kpi = dashboard_data.fetch_kpi_data(conn)
        assert kpi["tasks_completed"] == 2
        assert kpi["tasks_total"] == 3

    def test_avg_cost_per_task_correct(self):
        """avg_cost_per_task = total_cost / tasks_completed (not tasks_total)."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason) VALUES (?, ?, ?, ?)",
            (1, "done", "Done", "completed"),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 00:00:00", 0.30),
        )
        conn.commit()

        kpi = dashboard_data.fetch_kpi_data(conn)
        assert abs(kpi["avg_cost_per_task"] - 0.30) < 1e-9

    def test_avg_cost_per_task_zero_when_no_completed_tasks(self):
        """avg_cost_per_task is 0 when no tasks are Done."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "todo"))
        conn.commit()

        kpi = dashboard_data.fetch_kpi_data(conn)
        assert kpi["avg_cost_per_task"] == 0

    def test_empty_database_returns_zeros(self):
        """fetch_kpi_data() handles an empty database gracefully."""
        conn = _make_conn()
        kpi = dashboard_data.fetch_kpi_data(conn)
        assert kpi["total_cost"] == 0
        assert kpi["total_tokens_in"] == 0
        assert kpi["total_tokens_out"] == 0
        assert kpi["total_tokens"] == 0
        assert kpi["tasks_completed"] == 0
        assert kpi["tasks_total"] == 0
        assert kpi["avg_cost_per_task"] == 0


# ---------------------------------------------------------------------------
# fetch_complexity_metrics()
# ---------------------------------------------------------------------------


class TestFetchComplexityMetrics:
    def test_groups_by_complexity(self):
        """fetch_complexity_metrics() returns one row per complexity bucket."""
        conn = _make_conn()
        for i, complexity in enumerate(["XS", "S", "M"], start=1):
            conn.execute(
                "INSERT INTO tasks (id, summary, status, closed_reason, complexity) VALUES (?, ?, ?, ?, ?)",
                (i, f"task {complexity}", "Done", "completed", complexity),
            )
        conn.commit()

        rows = dashboard_data.fetch_complexity_metrics(conn)
        complexities = [r["complexity"] for r in rows]
        assert set(complexities) == {"XS", "S", "M"}

    def test_ordered_by_complexity_size(self):
        """fetch_complexity_metrics() returns rows in XS→S→M→L→XL order."""
        conn = _make_conn()
        for i, c in enumerate(["L", "XS", "M", "S", "XL"], start=1):
            conn.execute(
                "INSERT INTO tasks (id, summary, status, closed_reason, complexity) VALUES (?, ?, ?, ?, ?)",
                (i, f"task {c}", "Done", "completed", c),
            )
        conn.commit()

        rows = dashboard_data.fetch_complexity_metrics(conn)
        complexities = [r["complexity"] for r in rows]
        assert complexities == ["XS", "S", "M", "L", "XL"]

    def test_excludes_non_done_tasks(self):
        """fetch_complexity_metrics() only includes Done tasks."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, complexity) VALUES (?, ?, ?, ?, ?)",
            (1, "done", "Done", "completed", "S"),
        )
        conn.execute(
            "INSERT INTO tasks (id, summary, status, complexity) VALUES (?, ?, ?, ?)",
            (2, "todo", "To Do", "S"),
        )
        conn.commit()

        rows = dashboard_data.fetch_complexity_metrics(conn)
        assert len(rows) == 1
        assert rows[0]["task_count"] == 1

    def test_avg_sessions_computed_correctly(self):
        """avg_sessions is the mean session_count for tasks in each complexity bucket."""
        conn = _make_conn()
        # Two M tasks: one with 1 session, one with 3 sessions → avg = 2.0
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, complexity) VALUES (?, ?, ?, ?, ?)",
            (1, "m task 1", "Done", "completed", "M"),
        )
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, complexity) VALUES (?, ?, ?, ?, ?)",
            (2, "m task 2", "Done", "completed", "M"),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, duration_seconds, cost_dollars) VALUES (?, ?, ?, ?)",
            (1, "2026-01-01 00:00:00", 100, 0.01),
        )
        for j in range(3):
            conn.execute(
                "INSERT INTO task_sessions (task_id, started_at, duration_seconds, cost_dollars) VALUES (?, ?, ?, ?)",
                (2, f"2026-01-0{j+1} 00:00:00", 200, 0.02),
            )
        conn.commit()

        rows = dashboard_data.fetch_complexity_metrics(conn)
        m_row = next(r for r in rows if r["complexity"] == "M")
        assert m_row["avg_sessions"] == 2.0

    def test_empty_database_returns_no_rows(self):
        """fetch_complexity_metrics() returns empty list when no Done tasks exist."""
        conn = _make_conn()
        rows = dashboard_data.fetch_complexity_metrics(conn)
        assert rows == []


# ---------------------------------------------------------------------------
# Extended schema helpers (tables/views not in the base _SCHEMA)
# ---------------------------------------------------------------------------

_CRITERIA_TABLE = """
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT NOT NULL,
    is_completed INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    cost_dollars REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    completed_at TEXT,
    criterion_type TEXT,
    commit_hash TEXT,
    committed_at TEXT
);
"""

_DEPENDENCIES_TABLE = """
CREATE TABLE task_dependencies (
    task_id INTEGER NOT NULL,
    depends_on_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL DEFAULT 'blocks'
);
"""

_SKILL_RUNS_TABLE = """
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    cost_dollars REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    model TEXT,
    metadata TEXT,
    request_count INTEGER,
    task_id INTEGER
);
"""

_VELOCITY_VIEW = """
CREATE VIEW v_velocity AS
SELECT date(t.closed_at, 'weekday 0', '-6 days') as week,
       COUNT(*) as task_count,
       COALESCE(AVG(s.sess_cost), 0.0) as avg_cost
FROM tasks t
LEFT JOIN (
    SELECT task_id, SUM(cost_dollars) as sess_cost
    FROM task_sessions
    GROUP BY task_id
) s ON s.task_id = t.id
WHERE t.status = 'Done' AND t.closed_at IS NOT NULL
GROUP BY week;
"""


def _add_criteria_table(conn):
    """Add acceptance_criteria table to an existing in-memory connection."""
    conn.executescript(_CRITERIA_TABLE)
    return conn


def _add_dependencies_table(conn):
    """Add task_dependencies table to an existing in-memory connection."""
    conn.executescript(_DEPENDENCIES_TABLE)
    return conn


def _add_skill_runs_table(conn):
    """Add skill_runs table to an existing in-memory connection."""
    conn.executescript(_SKILL_RUNS_TABLE)
    return conn


def _add_velocity_view(conn):
    """Add v_velocity view to an existing in-memory connection."""
    conn.executescript(_VELOCITY_VIEW)
    return conn


def _make_conn_full():
    """In-memory connection with all tables including acceptance_criteria,
    task_dependencies, skill_runs, and the v_velocity view."""
    conn = _make_conn()
    _add_criteria_table(conn)
    _add_dependencies_table(conn)
    _add_skill_runs_table(conn)
    _add_velocity_view(conn)
    return conn


# ---------------------------------------------------------------------------
# fetch_all_criteria()
# ---------------------------------------------------------------------------


class TestFetchAllCriteria:
    def test_groups_by_task_id(self):
        """Results are keyed by task_id with one list per task."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task A"))
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (2, "task B"))
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (1, "crit A1", 0),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (1, "crit A2", 1),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (2, "crit B1", 0),
        )
        conn.commit()

        result = dashboard_data.fetch_all_criteria(conn)
        assert set(result.keys()) == {1, 2}
        assert len(result[1]) == 2
        assert len(result[2]) == 1

    def test_criteria_ordered_by_id_within_task(self):
        """Criteria within a task are ordered by id ASC."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (1, "first inserted", 0),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (1, "second inserted", 0),
        )
        conn.commit()

        result = dashboard_data.fetch_all_criteria(conn)
        assert result[1][0]["criterion"] == "first inserted"
        assert result[1][1]["criterion"] == "second inserted"

    def test_returns_empty_dict_when_no_criteria(self):
        conn = _make_conn_full()
        result = dashboard_data.fetch_all_criteria(conn)
        assert result == {}

    def test_task_with_no_criteria_absent_from_result(self):
        """A task with no acceptance_criteria rows does not appear as a key in the result."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task no criteria"))
        conn.commit()

        result = dashboard_data.fetch_all_criteria(conn)
        assert 1 not in result
        assert result == {}

    def test_is_completed_field_preserved(self):
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (1, "done crit", 1),
        )
        conn.commit()

        result = dashboard_data.fetch_all_criteria(conn)
        assert result[1][0]["is_completed"] == 1


# ---------------------------------------------------------------------------
# fetch_task_dependencies()
# ---------------------------------------------------------------------------


class TestFetchTaskDependencies:
    def test_builds_blocked_by_and_blocks(self):
        """A depends_on B → A.blocked_by=[B], B.blocks=[A]."""
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type) VALUES (?, ?, ?)",
            (1, 2, "blocks"),
        )
        conn.commit()

        result = dashboard_data.fetch_task_dependencies(conn)
        assert 1 in result
        assert 2 in result
        assert result[1]["blocked_by"] == [{"id": 2, "type": "blocks"}]
        assert result[1]["blocks"] == []
        assert result[2]["blocked_by"] == []
        assert result[2]["blocks"] == [{"id": 1, "type": "blocks"}]

    def test_multiple_dependencies(self):
        """Task blocked by two tasks has both in blocked_by list."""
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type) VALUES (?, ?, ?)",
            (1, 2, "blocks"),
        )
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type) VALUES (?, ?, ?)",
            (1, 3, "contingent"),
        )
        conn.commit()

        result = dashboard_data.fetch_task_dependencies(conn)
        blocked_by_ids = {d["id"] for d in result[1]["blocked_by"]}
        assert blocked_by_ids == {2, 3}

    def test_empty_table_returns_empty_dict(self):
        conn = _make_conn_full()
        result = dashboard_data.fetch_task_dependencies(conn)
        assert result == {}

    def test_relationship_type_preserved(self):
        """relationship_type from the row is forwarded into the dicts."""
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type) VALUES (?, ?, ?)",
            (1, 2, "contingent"),
        )
        conn.commit()

        result = dashboard_data.fetch_task_dependencies(conn)
        assert result[1]["blocked_by"][0]["type"] == "contingent"
        assert result[2]["blocks"][0]["type"] == "contingent"


# ---------------------------------------------------------------------------
# fetch_skill_runs()
# ---------------------------------------------------------------------------


class TestFetchSkillRuns:
    def test_happy_path_returns_rows_newest_first(self):
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, cost_dollars) VALUES (?, ?, ?)",
            ("review-commits", "2026-01-01 00:00:00", 0.02),
        )
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, cost_dollars) VALUES (?, ?, ?)",
            ("tusk", "2026-01-02 00:00:00", 0.05),
        )
        conn.commit()

        rows = dashboard_data.fetch_skill_runs(conn)
        assert len(rows) == 2
        assert rows[0]["skill_name"] == "tusk"
        assert rows[1]["skill_name"] == "review-commits"

    def test_missing_table_returns_empty_list(self):
        """OperationalError when skill_runs table is absent → returns []."""
        conn = _make_conn()  # no skill_runs table
        rows = dashboard_data.fetch_skill_runs(conn)
        assert rows == []

    def test_empty_table_returns_empty_list(self):
        conn = _make_conn_full()
        rows = dashboard_data.fetch_skill_runs(conn)
        assert rows == []

    def test_fields_returned_correctly(self):
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, cost_dollars, tokens_in, tokens_out, model) VALUES (?, ?, ?, ?, ?, ?)",
            ("tusk", "2026-01-01 10:00:00", 0.10, 1000, 500, "claude-sonnet-4"),
        )
        conn.commit()

        rows = dashboard_data.fetch_skill_runs(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["skill_name"] == "tusk"
        assert abs(r["cost_dollars"] - 0.10) < 1e-9
        assert r["tokens_in"] == 1000
        assert r["tokens_out"] == 500
        assert r["model"] == "claude-sonnet-4"


# ---------------------------------------------------------------------------
# fetch_model_performance()
# ---------------------------------------------------------------------------


class TestFetchModelPerformance:
    def test_unions_task_and_skill_models(self):
        """Models are drawn from task_sessions and skill_runs; each gets a row."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "t1"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model, request_count) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 0.50, "sonnet-4", 30),
        )
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, cost_dollars, model, request_count) VALUES (?, ?, ?, ?, ?)",
            ("/tusk", "2026-04-01 11:00:00", 0.05, "haiku-4", 5),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        names = {m["model"] for m in result["models"]}
        assert names == {"sonnet-4", "haiku-4"}

    def test_task_and_skill_rollups_are_separate(self):
        """Same model on both tables → one row with both sub-aggregates."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "t1"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, tokens_in, tokens_out, lines_added, lines_removed, model, request_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 0.80, 1000, 500, 30, 10, "sonnet-4", 40),
        )
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, cost_dollars, tokens_in, tokens_out, model, request_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("/tusk", "2026-04-01 11:00:00", 0.20, 200, 100, "sonnet-4", 7),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        assert len(result["models"]) == 1
        m = result["models"][0]
        assert m["model"] == "sonnet-4"
        assert m["task_session_count"] == 1
        assert abs(m["task_cost"] - 0.80) < 1e-9
        assert m["task_tokens_in"] == 1000
        assert m["task_lines_added"] == 30
        assert m["task_request_count"] == 40
        assert m["skill_run_count"] == 1
        assert abs(m["skill_cost"] - 0.20) < 1e-9
        assert m["skill_tokens_in"] == 200
        assert m["skill_request_count"] == 7

    def test_null_model_becomes_unknown(self):
        """Rows with NULL model group under the sentinel 'unknown'."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "t1"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 0.10, None),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        assert [m["model"] for m in result["models"]] == ["unknown"]

    def test_empty_string_model_merges_with_unknown(self):
        """Sessions with model='' and model=NULL must land in the same 'unknown' bucket."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "t1"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 0.10, ""),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            (1, "2026-04-01 11:00:00", 0.20, None),
        )
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            ("/tusk", "2026-04-01 12:00:00", 0.03, ""),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        assert [m["model"] for m in result["models"]] == ["unknown"]
        row = result["models"][0]
        assert row["task_session_count"] == 2
        assert row["skill_run_count"] == 1

    def test_models_sorted_by_total_cost_desc(self):
        """Highest task_cost + skill_cost combined model appears first."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "t1"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 0.10, "cheap-model"),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            (1, "2026-04-01 11:00:00", 5.00, "expensive-model"),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        assert result["models"][0]["model"] == "expensive-model"
        assert result["models"][1]["model"] == "cheap-model"

    def test_null_request_count_surfaces_as_none_not_zero(self):
        """Pre-migration rows with NULL request_count must not be coerced to 0 in rollups.

        The Models tab renders '\u2014 turns' when request_count is None (unknown)
        and 'N turns' when it's a real number. Collapsing NULL->0 at the SQL
        layer makes 'N tokens but 0 turns' impossible to diagnose.
        """
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO tasks (id, summary, complexity) VALUES (?, ?, ?)",
            (1, "t1", "M"),
        )
        # Task-side: explicit NULL request_count (simulates pre-TASK-73 migration row).
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, tokens_in, model, request_count) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 0.50, 1000, "sonnet-4", None),
        )
        # Skill-side: explicit NULL request_count.
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, cost_dollars, model, request_count) VALUES (?, ?, ?, ?, ?)",
            ("/tusk", "2026-04-01 11:00:00", 0.05, "sonnet-4", None),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)

        assert len(result["models"]) == 1
        m = result["models"][0]
        assert m["task_request_count"] is None, "task rollup must preserve NULL"
        assert m["skill_request_count"] is None, "skill rollup must preserve NULL"

        cm = result["complexity_matrix"]
        assert len(cm) == 1
        assert cm[0]["avg_turns"] is None, "complexity matrix avg_turns must preserve NULL"

        ts_task = result["timeseries_tasks"]
        assert len(ts_task) == 1
        assert ts_task[0]["request_count"] is None, "task timeseries request_count must preserve NULL"

        ts_skill = result["timeseries_skills"]
        assert len(ts_skill) == 1
        assert ts_skill[0]["request_count"] is None, "skill timeseries request_count must preserve NULL"

    def test_mixed_null_and_real_request_count_sums_known_values(self):
        """When some rows have NULL request_count and others don't, the rollup sums only the known values."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary, complexity) VALUES (?, ?, ?)", (1, "t1", "M"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model, request_count) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 0.50, "sonnet-4", None),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model, request_count) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-04-01 11:00:00", 0.50, "sonnet-4", 20),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        assert result["models"][0]["task_request_count"] == 20
        assert result["complexity_matrix"][0]["avg_turns"] == 20.0

    def test_complexity_matrix_groups_by_model_and_complexity(self):
        """complexity_matrix carries avg_turns and avg_cost per (model, complexity)."""
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, complexity) VALUES (?, ?, ?, ?, ?)",
            (1, "m-task", "Done", "completed", "M"),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model, request_count) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 1.00, "sonnet-4", 100),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model, request_count) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-04-01 11:00:00", 0.50, "sonnet-4", 50),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        cm = result["complexity_matrix"]
        assert len(cm) == 1
        row = cm[0]
        assert row["model"] == "sonnet-4"
        assert row["complexity"] == "M"
        assert row["session_count"] == 2
        assert row["avg_turns"] == 75.0
        assert abs(row["avg_cost"] - 0.75) < 1e-9

    def test_complexity_matrix_excludes_null_complexity(self):
        """Sessions linked to tasks with NULL complexity are omitted."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary, complexity) VALUES (?, ?, ?)", (1, "no-size", None))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 1.00, "sonnet-4"),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        assert result["complexity_matrix"] == []

    def test_timeseries_bucketed_by_local_day(self):
        """offset_minutes shifts started_at into local day buckets."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "t1"))
        # 04-01 23:30 UTC → 04-02 00:30 at UTC+60
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            (1, "2026-04-01 23:30:00", 1.00, "sonnet-4"),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn, offset_minutes=60)
        assert len(result["timeseries_tasks"]) == 1
        assert result["timeseries_tasks"][0]["day"] == "2026-04-02"
        assert result["timeseries_tasks"][0]["model"] == "sonnet-4"

    def test_missing_skill_runs_table_degrades_gracefully(self):
        """No skill_runs table → skill rollups and timeseries are empty, task side still works."""
        conn = _make_conn()  # minimal schema, no skill_runs
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "t1"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars, model) VALUES (?, ?, ?, ?)",
            (1, "2026-04-01 10:00:00", 0.25, "sonnet-4"),
        )
        conn.commit()

        result = dashboard_data.fetch_model_performance(conn)
        assert len(result["models"]) == 1
        assert result["models"][0]["skill_run_count"] == 0
        assert abs(result["models"][0]["skill_cost"]) < 1e-9
        assert result["timeseries_skills"] == []

    def test_empty_database_returns_empty_lists(self):
        conn = _make_conn_full()
        result = dashboard_data.fetch_model_performance(conn)
        assert result == {
            "models": [],
            "complexity_matrix": [],
            "timeseries_tasks": [],
            "timeseries_skills": [],
        }


# ---------------------------------------------------------------------------
# fetch_velocity()
# ---------------------------------------------------------------------------


class TestFetchVelocity:
    def test_missing_view_returns_empty_list(self):
        """OperationalError when v_velocity view is absent → returns []."""
        conn = _make_conn()  # no v_velocity view
        rows = dashboard_data.fetch_velocity(conn)
        assert rows == []

    def test_happy_path_returns_oldest_first(self):
        """fetch_velocity reverses DESC query so result is oldest-first."""
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, closed_at) VALUES (?, ?, ?, ?, ?)",
            (1, "task1", "Done", "completed", "2026-01-05 12:00:00"),
        )
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, closed_at) VALUES (?, ?, ?, ?, ?)",
            (2, "task2", "Done", "completed", "2026-02-02 12:00:00"),
        )
        conn.commit()

        rows = dashboard_data.fetch_velocity(conn)
        assert len(rows) == 2
        assert rows[0]["week"] <= rows[1]["week"]

    def test_task_count_per_week(self):
        """Two tasks in the same week → task_count = 2."""
        conn = _make_conn_full()
        # Both tasks closed in the week of 2026-01-05 (Mon–Sun)
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, closed_at) VALUES (?, ?, ?, ?, ?)",
            (1, "t1", "Done", "completed", "2026-01-06 00:00:00"),
        )
        conn.execute(
            "INSERT INTO tasks (id, summary, status, closed_reason, closed_at) VALUES (?, ?, ?, ?, ?)",
            (2, "t2", "Done", "completed", "2026-01-07 00:00:00"),
        )
        conn.commit()

        rows = dashboard_data.fetch_velocity(conn)
        assert len(rows) == 1
        assert rows[0]["task_count"] == 2

    def test_excludes_non_done_tasks(self):
        """In Progress tasks do not appear in velocity (view filters status='Done')."""
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO tasks (id, summary, status) VALUES (?, ?, ?)",
            (1, "wip", "In Progress"),
        )
        conn.commit()

        rows = dashboard_data.fetch_velocity(conn)
        assert rows == []


# ---------------------------------------------------------------------------
# fetch_hourly_cost()
# ---------------------------------------------------------------------------


class TestFetchHourlyCost:
    def test_always_returns_24_buckets(self):
        conn = _make_conn()
        result = dashboard_data.fetch_hourly_cost(conn)
        assert len(result) == 24

    def test_zero_fill_when_no_sessions(self):
        conn = _make_conn()
        result = dashboard_data.fetch_hourly_cost(conn)
        assert all(r["cost_tasks"] == 0.0 for r in result)
        assert all(r["cost_skills"] == 0.0 for r in result)

    def test_buckets_indexed_0_to_23(self):
        conn = _make_conn()
        result = dashboard_data.fetch_hourly_cost(conn)
        assert [r["hour"] for r in result] == list(range(24))

    def test_task_cost_placed_in_correct_hour(self):
        """Session started at 14:xx UTC lands in hour 14 with offset 0."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 14:30:00", 0.10),
        )
        conn.commit()

        result = dashboard_data.fetch_hourly_cost(conn, offset_minutes=0)
        assert abs(result[14]["cost_tasks"] - 0.10) < 1e-9
        assert result[13]["cost_tasks"] == 0.0
        assert result[15]["cost_tasks"] == 0.0

    def test_offset_minutes_shifts_bucket(self):
        """Session at 23:30 UTC + 60 min offset → lands in hour 0."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 23:30:00", 0.05),
        )
        conn.commit()

        result_utc = dashboard_data.fetch_hourly_cost(conn, offset_minutes=0)
        assert abs(result_utc[23]["cost_tasks"] - 0.05) < 1e-9

        result_offset = dashboard_data.fetch_hourly_cost(conn, offset_minutes=60)
        assert abs(result_offset[0]["cost_tasks"] - 0.05) < 1e-9
        assert result_offset[23]["cost_tasks"] == 0.0

    def test_negative_offset_minutes(self):
        """Session at 00:30 UTC with -60 min offset → lands in hour 23."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-02 00:30:00", 0.07),
        )
        conn.commit()

        result = dashboard_data.fetch_hourly_cost(conn, offset_minutes=-60)
        assert abs(result[23]["cost_tasks"] - 0.07) < 1e-9
        assert result[0]["cost_tasks"] == 0.0

    def test_missing_skill_runs_table_still_returns_24_buckets(self):
        """Absent skill_runs → cost_skills stays 0 for all hours, no exception."""
        conn = _make_conn()  # no skill_runs table
        result = dashboard_data.fetch_hourly_cost(conn)
        assert len(result) == 24
        assert all(r["cost_skills"] == 0.0 for r in result)

    def test_skill_costs_placed_in_correct_hour(self):
        """Skill run cost lands in the right hour bucket."""
        conn = _make_conn_full()
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, cost_dollars) VALUES (?, ?, ?)",
            ("tusk", "2026-01-01 09:15:00", 0.03),
        )
        conn.commit()

        result = dashboard_data.fetch_hourly_cost(conn, offset_minutes=0)
        assert abs(result[9]["cost_skills"] - 0.03) < 1e-9
        assert result[9]["cost_tasks"] == 0.0


# ---------------------------------------------------------------------------
# fetch_dag_tasks()
# ---------------------------------------------------------------------------


class TestFetchDagTasks:
    def test_criteria_total_and_done_counts(self):
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (1, "c1", 1),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (1, "c2", 0),
        )
        conn.commit()

        rows = dashboard_data.fetch_dag_tasks(conn)
        assert len(rows) == 1
        assert rows[0]["criteria_total"] == 2
        assert rows[0]["criteria_done"] == 1

    def test_task_with_no_criteria_has_zero_counts(self):
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.commit()

        rows = dashboard_data.fetch_dag_tasks(conn)
        assert len(rows) == 1
        assert rows[0]["criteria_total"] == 0
        assert rows[0]["criteria_done"] == 0

    def test_ordered_by_task_id_asc(self):
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (3, "task C"))
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task A"))
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (2, "task B"))
        conn.commit()

        rows = dashboard_data.fetch_dag_tasks(conn)
        assert [r["id"] for r in rows] == [1, 2, 3]

    def test_multiple_tasks_criteria_not_cross_contaminated(self):
        """Criteria for task 2 do not appear in task 1's counts."""
        conn = _make_conn_full()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task A"))
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (2, "task B"))
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (1, "c1", 1),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (2, "c2", 0),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, ?, ?)",
            (2, "c3", 1),
        )
        conn.commit()

        rows = dashboard_data.fetch_dag_tasks(conn)
        by_id = {r["id"]: r for r in rows}
        assert by_id[1]["criteria_total"] == 1
        assert by_id[1]["criteria_done"] == 1
        assert by_id[2]["criteria_total"] == 2
        assert by_id[2]["criteria_done"] == 1


# ---------------------------------------------------------------------------
# fetch_cost_trend_daily()
# ---------------------------------------------------------------------------


class TestFetchCostTrendDaily:
    def test_groups_by_day_and_sums(self):
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 10:00:00", 0.10),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 20:00:00", 0.20),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-02 10:00:00", 0.05),
        )
        conn.commit()

        rows = dashboard_data.fetch_cost_trend_daily(conn)
        assert len(rows) == 2
        assert rows[0]["day"] == "2026-01-01"
        assert abs(rows[0]["daily_cost"] - 0.30) < 1e-9
        assert rows[1]["day"] == "2026-01-02"
        assert abs(rows[1]["daily_cost"] - 0.05) < 1e-9

    def test_ordered_oldest_first(self):
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-03-01 00:00:00", 0.10),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 00:00:00", 0.05),
        )
        conn.commit()

        rows = dashboard_data.fetch_cost_trend_daily(conn)
        assert len(rows) == 2
        assert rows[0]["day"] < rows[1]["day"]

    def test_excludes_zero_cost_sessions(self):
        """Sessions with cost_dollars = 0 are excluded (WHERE cost_dollars > 0)."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 00:00:00", 0.0),
        )
        conn.commit()

        rows = dashboard_data.fetch_cost_trend_daily(conn)
        assert rows == []

    def test_empty_database_returns_empty_list(self):
        conn = _make_conn()
        rows = dashboard_data.fetch_cost_trend_daily(conn)
        assert rows == []

    def test_offset_minutes_applied(self):
        """offset_minutes shifts the date bucket."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        # 23:00 UTC; with +120 min offset → 01:00 next day
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 23:00:00", 0.10),
        )
        conn.commit()

        rows_utc = dashboard_data.fetch_cost_trend_daily(conn, offset_minutes=0)
        assert rows_utc[0]["day"] == "2026-01-01"

        rows_shifted = dashboard_data.fetch_cost_trend_daily(conn, offset_minutes=120)
        assert rows_shifted[0]["day"] == "2026-01-02"


# ---------------------------------------------------------------------------
# fetch_cost_trend_monthly()
# ---------------------------------------------------------------------------


class TestFetchCostTrendMonthly:
    def test_groups_by_month_and_sums(self):
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-05 00:00:00", 0.10),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-20 00:00:00", 0.15),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-02-01 00:00:00", 0.08),
        )
        conn.commit()

        rows = dashboard_data.fetch_cost_trend_monthly(conn)
        by_month = {r["month"]: r for r in rows}
        assert set(by_month.keys()) == {"2026-01", "2026-02"}
        assert abs(by_month["2026-01"]["monthly_cost"] - 0.25) < 1e-9
        assert abs(by_month["2026-02"]["monthly_cost"] - 0.08) < 1e-9

    def test_ordered_oldest_first(self):
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-03-01 00:00:00", 0.10),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 00:00:00", 0.05),
        )
        conn.commit()

        rows = dashboard_data.fetch_cost_trend_monthly(conn)
        assert len(rows) == 2
        assert rows[0]["month"] < rows[1]["month"]

    def test_excludes_zero_cost_sessions(self):
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-01 00:00:00", 0.0),
        )
        conn.commit()

        rows = dashboard_data.fetch_cost_trend_monthly(conn)
        assert rows == []

    def test_empty_database_returns_empty_list(self):
        conn = _make_conn()
        rows = dashboard_data.fetch_cost_trend_monthly(conn)
        assert rows == []

    def test_offset_minutes_applied(self):
        """offset_minutes shifts the month bucket (e.g. 23:00 UTC + 120 min → next day/month)."""
        conn = _make_conn()
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (1, "task"))
        # 23:00 UTC on the last day of January; with +120 min offset → 01:00 Feb 1
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cost_dollars) VALUES (?, ?, ?)",
            (1, "2026-01-31 23:00:00", 0.12),
        )
        conn.commit()

        rows_utc = dashboard_data.fetch_cost_trend_monthly(conn, offset_minutes=0)
        assert rows_utc[0]["month"] == "2026-01"

        rows_shifted = dashboard_data.fetch_cost_trend_monthly(conn, offset_minutes=120)
        assert rows_shifted[0]["month"] == "2026-02"


# ---------------------------------------------------------------------------
# Schema sync guard: _SCHEMA fixture vs bin/tusk CREATE TABLE task_sessions
# ---------------------------------------------------------------------------

def _extract_task_sessions_columns(sql_text):
    """Return the set of column names defined in the CREATE TABLE task_sessions block.

    Scans line-by-line from the opening '(' to the closing ');' so that column
    definitions containing sub-expressions (e.g. DEFAULT (datetime('now'))) do
    not prematurely terminate the match.
    """
    import re

    header = re.search(r"CREATE TABLE task_sessions\s*\(", sql_text, re.IGNORECASE)
    if not header:
        return set()

    body_start = sql_text.index("(", header.start())
    body_lines = []
    for line in sql_text[body_start + 1:].splitlines():
        if line.strip().startswith(")"):
            break
        body_lines.append(line)

    columns = set()
    for line in body_lines:
        line = line.strip().rstrip(",")
        if not line:
            continue
        # Skip constraint/key lines
        if re.match(r"(FOREIGN KEY|PRIMARY KEY|UNIQUE|CHECK|CONSTRAINT)\b", line, re.IGNORECASE):
            continue
        col_match = re.match(r"(\w+)", line)
        if col_match:
            columns.add(col_match.group(1).lower())
    return columns


class TestTaskSessionsSchemaSync:
    """Guard against drift between _SCHEMA fixture and bin/tusk CREATE TABLE task_sessions."""

    def test_fixture_matches_bin_tusk(self):
        """Fail if any column is present in one definition but absent from the other.

        This catches future migrations that add columns to task_sessions without
        updating the _SCHEMA test fixture (the scenario that triggered TASK-636).
        """
        tusk_path = os.path.join(REPO_ROOT, "bin", "tusk")
        with open(tusk_path) as f:
            tusk_sql = f.read()

        tusk_cols = _extract_task_sessions_columns(tusk_sql)
        fixture_cols = _extract_task_sessions_columns(_SCHEMA)

        assert tusk_cols, "Could not find CREATE TABLE task_sessions in bin/tusk"
        assert fixture_cols, "Could not find CREATE TABLE task_sessions in _SCHEMA fixture"

        missing_from_fixture = tusk_cols - fixture_cols
        extra_in_fixture = fixture_cols - tusk_cols

        assert not missing_from_fixture, (
            f"task_sessions columns in bin/tusk missing from _SCHEMA fixture: {sorted(missing_from_fixture)}. "
            "Update _SCHEMA in tests/unit/test_dashboard_data.py to match."
        )
        assert not extra_in_fixture, (
            f"task_sessions columns in _SCHEMA fixture not in bin/tusk: {sorted(extra_in_fixture)}. "
            "Update _SCHEMA in tests/unit/test_dashboard_data.py to match."
        )

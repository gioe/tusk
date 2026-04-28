"""Unit tests for fetch_baseline_comparison in tusk-task-summary.py.

Covers the four status outcomes called out by TASK-244 criterion 1082:
- bucket >= threshold (status='compared', ratio populated)
- bucket below threshold (status='pending', ratio=None)
- null complexity on the current task (status='no_complexity')
- no peers in the bucket (status='no_peers', n=0)

Plus the supporting invariants that make those outcomes correct:
- median over an even-count peer set averages the middle two
- non-completed closed peers (wont_do/duplicate) and zero-cost peers are excluded
- the current task is excluded from its own peer set
"""

import importlib.util
import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_task_summary",
    os.path.join(BIN, "tusk-task-summary.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# Minimal schema — only columns fetch_baseline_comparison reads.
_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    status TEXT,
    closed_reason TEXT,
    complexity TEXT
);
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    cost_dollars REAL
);
"""


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _insert_peer(conn, task_id, complexity, cost,
                 status="Done", closed_reason="completed"):
    conn.execute(
        "INSERT INTO tasks (id, status, closed_reason, complexity) VALUES (?, ?, ?, ?)",
        (task_id, status, closed_reason, complexity),
    )
    if cost is not None:
        conn.execute(
            "INSERT INTO skill_runs (task_id, cost_dollars) VALUES (?, ?)",
            (task_id, cost),
        )
    conn.commit()


# ── status='compared' ─────────────────────────────────────────────────


class TestCompared:
    def test_odd_count_median(self):
        conn = _db()
        _insert_peer(conn, 2, "M", 0.10)
        _insert_peer(conn, 3, "M", 0.20)
        _insert_peer(conn, 4, "M", 0.30)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="M",
                                               current_cost=0.30, threshold=3)
        assert result["status"] == "compared"
        assert result["bucket"] == "M"
        assert result["n"] == 3
        assert result["median_cost"] == 0.20
        assert result["ratio"] == 1.5  # 0.30 / 0.20
        assert result["threshold"] == 3

    def test_even_count_median_averages_middle_two(self):
        conn = _db()
        for tid, cost in [(2, 0.10), (3, 0.20), (4, 0.40), (5, 0.80)]:
            _insert_peer(conn, tid, "S", cost)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="S",
                                               current_cost=0.30, threshold=4)
        # median of [0.10, 0.20, 0.40, 0.80] = (0.20 + 0.40) / 2 = 0.30
        assert result["status"] == "compared"
        assert result["median_cost"] == 0.30
        assert result["ratio"] == 1.0
        assert result["n"] == 4

    def test_excludes_current_task_from_peer_set(self):
        conn = _db()
        # current task is also Done/completed/M — must not be counted as its own peer
        _insert_peer(conn, 1, "M", 0.50)
        _insert_peer(conn, 2, "M", 0.10)
        _insert_peer(conn, 3, "M", 0.20)
        _insert_peer(conn, 4, "M", 0.30)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="M",
                                               current_cost=0.50, threshold=3)
        assert result["n"] == 3
        assert result["median_cost"] == 0.20

    def test_excludes_non_completed_closed_reasons(self):
        conn = _db()
        _insert_peer(conn, 2, "M", 0.10)
        _insert_peer(conn, 3, "M", 0.20)
        _insert_peer(conn, 4, "M", 0.30)
        # wont_do and duplicate peers must not pollute the bucket
        _insert_peer(conn, 5, "M", 5.00, closed_reason="wont_do")
        _insert_peer(conn, 6, "M", 5.00, closed_reason="duplicate")
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="M",
                                               current_cost=0.30, threshold=3)
        assert result["n"] == 3
        assert result["median_cost"] == 0.20

    def test_excludes_zero_cost_peers(self):
        conn = _db()
        _insert_peer(conn, 2, "M", 0.10)
        _insert_peer(conn, 3, "M", 0.20)
        _insert_peer(conn, 4, "M", 0.30)
        # zero-cost row in skill_runs (or no skill_runs at all) — drop it
        _insert_peer(conn, 5, "M", 0.0)
        _insert_peer(conn, 6, "M", None)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="M",
                                               current_cost=0.30, threshold=3)
        assert result["n"] == 3
        assert result["median_cost"] == 0.20

    def test_zero_current_cost_suppresses_ratio(self):
        # In-progress / not-yet-started tasks have current_cost == 0; the
        # bucket median + n should still ship in compared status, but the
        # multiplier is suppressed so the markdown does not mislead with
        # "0.0x baseline".
        conn = _db()
        _insert_peer(conn, 2, "M", 0.10)
        _insert_peer(conn, 3, "M", 0.20)
        _insert_peer(conn, 4, "M", 0.30)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="M",
                                               current_cost=0.0, threshold=3)
        assert result["status"] == "compared"
        assert result["n"] == 3
        assert result["median_cost"] == 0.20
        assert result["ratio"] is None


# ── status='pending' ──────────────────────────────────────────────────


class TestPending:
    def test_below_threshold(self):
        conn = _db()
        _insert_peer(conn, 2, "L", 0.10)
        _insert_peer(conn, 3, "L", 0.20)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="L",
                                               current_cost=0.30, threshold=10)
        assert result["status"] == "pending"
        assert result["bucket"] == "L"
        assert result["n"] == 2
        assert result["ratio"] is None  # no comparison yet
        assert result["median_cost"] == 0.15  # still computed for transparency
        assert result["threshold"] == 10


# ── status='no_complexity' ────────────────────────────────────────────


class TestNoComplexity:
    def test_current_task_has_null_complexity(self):
        conn = _db()
        # Even if peers exist in some bucket, an unbucketed current task can't compare
        _insert_peer(conn, 2, "M", 0.10)
        _insert_peer(conn, 3, "M", 0.20)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity=None,
                                               current_cost=0.30, threshold=10)
        assert result["status"] == "no_complexity"
        assert result["bucket"] is None
        assert result["median_cost"] is None
        assert result["n"] == 0
        assert result["ratio"] is None
        assert result["threshold"] == 10

    def test_empty_string_complexity_treated_as_none(self):
        conn = _db()
        _insert_peer(conn, 2, "M", 0.10)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="",
                                               current_cost=0.30, threshold=10)
        assert result["status"] == "no_complexity"


# ── status='no_peers' ─────────────────────────────────────────────────


class TestNoPeers:
    def test_first_task_in_bucket(self):
        conn = _db()
        # Other buckets populated, but XL is empty
        _insert_peer(conn, 2, "M", 0.10)
        _insert_peer(conn, 3, "S", 0.20)
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="XL",
                                               current_cost=1.00, threshold=10)
        assert result["status"] == "no_peers"
        assert result["bucket"] == "XL"
        assert result["n"] == 0
        assert result["median_cost"] is None
        assert result["ratio"] is None
        assert result["threshold"] == 10

    def test_no_peers_when_only_non_completed_exist(self):
        conn = _db()
        # Peers exist in the bucket but none are status='Done'+closed_reason='completed'
        _insert_peer(conn, 2, "M", 0.10, status="In Progress", closed_reason=None)
        _insert_peer(conn, 3, "M", 0.20, closed_reason="wont_do")
        _insert_peer(conn, 4, "M", 0.30, closed_reason="duplicate")
        result = mod.fetch_baseline_comparison(conn, task_id=1, complexity="M",
                                               current_cost=0.30, threshold=10)
        assert result["status"] == "no_peers"
        assert result["n"] == 0

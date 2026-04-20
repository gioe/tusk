"""Unit tests for tusk-review-wait.py.

The helper replaces the /review-commits Step 6 bash poll loop (``sleep 30`` +
``tusk review status``, repeated up to STALL_THRESHOLD iterations). Tests pin:

- Terminal status (approved, changes_requested, superseded) returns
  immediately with ``timed_out: false``.
- Still-pending status polls at ``interval`` until the timeout fires, then
  returns with ``timed_out: true`` and the final ``pending`` status.
- Status that transitions mid-wait (pending → approved at poll N) exits on
  that poll with the terminal payload.
- Nonexistent review_id exits 1 with a stderr message.
- CLI layer returns the expected JSON shape on stdout.

The tests stub ``time.sleep`` and ``time.monotonic`` so they run in
milliseconds without blocking on real wall-clock time.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
SCRIPT = os.path.join(BIN, "tusk-review-wait.py")


_spec = importlib.util.spec_from_file_location("tusk_review_wait", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── fixtures ───────────────────────────────────────────────────────────


def _make_db(tmp_path):
    """Minimal tasks.db with one task and one code_reviews row.

    Keeps only the columns the helper reads so this fixture does not need to
    track migrations to unrelated columns.
    """
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
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO tasks (id, summary) VALUES (1, 'sample');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_review(db_path, *, status="pending", reviewer="general", review_pass=1):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO code_reviews (task_id, reviewer, status, review_pass)"
        " VALUES (1, ?, ?, ?)",
        (reviewer, status, review_pass),
    )
    review_id = cur.lastrowid
    conn.commit()
    conn.close()
    return review_id


class _FakeClock:
    """Deterministic monotonic clock + sleep pair for tests.

    Each call to ``sleep(n)`` advances ``monotonic()`` by exactly ``n``
    without blocking. This lets us exercise the full timeout path in a few
    microseconds while still asserting the shape of the poll loop.
    """

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


# ── direct-function tests ──────────────────────────────────────────────


class TestTerminalStatuses:
    """Terminal statuses exit immediately with timed_out=False."""

    @pytest.mark.parametrize("status", ["approved", "changes_requested", "superseded"])
    def test_terminal_status_returns_immediately(self, tmp_path, status):
        db_path = _make_db(tmp_path)
        review_id = _insert_review(db_path, status=status)
        clock = _FakeClock()

        result = mod.wait_for_terminal(
            db_path, review_id,
            interval_seconds=30,
            timeout_seconds=150,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        assert result["status"] == status
        assert result["timed_out"] is False
        assert result["polls"] == 1
        assert result["review_id"] == review_id
        assert result["task_id"] == 1
        assert result["reviewer"] == "general"
        assert result["review_pass"] == 1
        # No sleeps on the fast path
        assert clock.sleeps == []


class TestTimeout:
    """Still-pending status polls until timeout, returns timed_out=True."""

    def test_pending_status_polls_until_timeout(self, tmp_path):
        db_path = _make_db(tmp_path)
        review_id = _insert_review(db_path, status="pending")
        clock = _FakeClock()

        result = mod.wait_for_terminal(
            db_path, review_id,
            interval_seconds=30,
            timeout_seconds=150,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        assert result["status"] == "pending"
        assert result["timed_out"] is True
        # 5 × 30s sleeps to reach 150s, then one more poll after the last
        # sleep sees elapsed >= timeout and exits.
        assert sum(clock.sleeps) == pytest.approx(150.0)
        assert result["elapsed_seconds"] == pytest.approx(150.0)
        assert result["polls"] >= 2

    def test_final_sleep_capped_to_remaining_time(self, tmp_path):
        """If interval > remaining, sleep should be clamped to remaining so
        we don't oversleep past the deadline by a full interval."""
        db_path = _make_db(tmp_path)
        review_id = _insert_review(db_path, status="pending")
        clock = _FakeClock()

        mod.wait_for_terminal(
            db_path, review_id,
            interval_seconds=100,
            timeout_seconds=150,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        # First poll at t=0 → sleep 100 (capped by interval).
        # Second poll at t=100 → sleep min(100, 50) = 50.
        # Third poll at t=150 → elapsed >= timeout → exit.
        assert clock.sleeps == [100.0, 50.0]


class TestStatusTransition:
    """Pending → terminal mid-wait exits on the poll that sees the change."""

    def test_exits_when_status_flips_to_terminal(self, tmp_path):
        db_path = _make_db(tmp_path)
        review_id = _insert_review(db_path, status="pending")
        clock = _FakeClock()

        # Flip to approved after the 2nd sleep (before the 3rd poll)
        real_fetch = mod._fetch_review
        poll_count = {"n": 0}

        def spy_fetch(db, rid):
            poll_count["n"] += 1
            if poll_count["n"] == 3:
                conn = sqlite3.connect(db)
                conn.execute(
                    "UPDATE code_reviews SET status = 'approved' WHERE id = ?",
                    (rid,),
                )
                conn.commit()
                conn.close()
            return real_fetch(db, rid)

        original = mod._fetch_review
        mod._fetch_review = spy_fetch
        try:
            result = mod.wait_for_terminal(
                db_path, review_id,
                interval_seconds=30,
                timeout_seconds=150,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )
        finally:
            mod._fetch_review = original

        assert result["status"] == "approved"
        assert result["timed_out"] is False
        assert result["polls"] == 3
        assert clock.sleeps == [30.0, 30.0]


class TestMissingReview:
    def test_missing_review_raises(self, tmp_path):
        db_path = _make_db(tmp_path)
        clock = _FakeClock()

        with pytest.raises(SystemExit) as exc:
            mod.wait_for_terminal(
                db_path, 9999,
                interval_seconds=30,
                timeout_seconds=150,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )
        assert "Review #9999 not found" in str(exc.value)


# ── CLI-layer tests ────────────────────────────────────────────────────


class TestCLI:
    def test_cli_returns_json_on_terminal(self, tmp_path):
        db_path = _make_db(tmp_path)
        review_id = _insert_review(db_path, status="approved")

        r = subprocess.run(
            [sys.executable, SCRIPT, db_path, "fake.json", str(review_id),
             "--interval", "1", "--timeout-seconds", "1"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert payload["status"] == "approved"
        assert payload["timed_out"] is False
        assert set(payload.keys()) == {
            "review_id", "task_id", "status", "review_pass", "reviewer",
            "timed_out", "elapsed_seconds", "polls",
        }

    def test_cli_rejects_missing_review(self, tmp_path):
        db_path = _make_db(tmp_path)

        r = subprocess.run(
            [sys.executable, SCRIPT, db_path, "fake.json", "9999",
             "--interval", "1", "--timeout-seconds", "1"],
            capture_output=True, text=True,
        )
        assert r.returncode == 1
        assert "Review #9999 not found" in r.stderr

    def test_cli_rejects_invalid_interval(self, tmp_path):
        db_path = _make_db(tmp_path)
        review_id = _insert_review(db_path, status="pending")

        r = subprocess.run(
            [sys.executable, SCRIPT, db_path, "fake.json", str(review_id),
             "--interval", "0"],
            capture_output=True, text=True,
        )
        assert r.returncode == 1
        assert "--interval" in r.stderr

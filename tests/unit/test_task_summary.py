"""Unit tests for tusk-task-summary.py.

Covers the four cases called out by task 118 criterion 527:
- one-session task (normal merge path, cost + duration + diff + criteria counts)
- multi-session task (sessions aggregate, earliest-start drives wall time)
- zero-commit abandon path (diff block is all zeros)
- reopen_count > 0 case (transitions surface in the summary)

Also exercises: TASK-N prefix resolution, task-not-found exit code, markdown
rendering singular/plural, and the diff filter's `[TASK-<id>]` fixed-string match.
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

_spec = importlib.util.spec_from_file_location(
    "tusk_task_summary",
    os.path.join(BIN, "tusk-task-summary.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── schema fixture ────────────────────────────────────────────────────
# Minimal subset of the real schema — only the columns this script queries.
# Not meant to mirror bin/tusk; no schema-sync guard is needed (see
# tests/unit/test_retro_signals.py for the same pattern).

_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    status TEXT DEFAULT 'To Do',
    closed_reason TEXT,
    started_at TEXT,
    closed_at TEXT
);
CREATE TABLE task_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds INTEGER
);
CREATE TABLE task_status_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL
);
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT,
    criterion_type TEXT DEFAULT 'manual',
    is_deferred INTEGER DEFAULT 0,
    skip_note TEXT
);
CREATE TABLE code_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    status TEXT DEFAULT 'pending'
);
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    task_id INTEGER,
    cost_dollars REAL
);
"""


def _make_db(tmp_path):
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(exist_ok=True)
    db_path = str(tusk_dir / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return db_path, conn


def _insert_task(conn, *, task_id, summary="Test", status="Done",
                 closed_reason="completed", started_at=None, closed_at=None):
    conn.execute(
        "INSERT INTO tasks (id, summary, status, closed_reason, started_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, summary, status, closed_reason, started_at, closed_at),
    )
    conn.commit()


def _run_main(db_path, task_id, fmt="json"):
    """Invoke the script end-to-end via subprocess (matches retro-signals harness)."""
    argv = [sys.executable, os.path.join(BIN, "tusk-task-summary.py"),
            db_path, "fake_config.json", str(task_id)]
    if fmt:
        argv.extend(["--format", fmt])
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
    return result.returncode, result.stdout, result.stderr


# ── helpers ───────────────────────────────────────────────────────────


class TestResolveTaskId:
    def test_plain_integer(self):
        assert mod._resolve_task_id("42") == 42

    def test_task_prefix(self):
        assert mod._resolve_task_id("TASK-42") == 42

    def test_task_prefix_lowercase(self):
        assert mod._resolve_task_id("task-42") == 42

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            mod._resolve_task_id("not-an-id")


class TestFormatDuration:
    def test_none(self):
        assert mod._format_duration(None) == "—"

    def test_seconds(self):
        assert mod._format_duration(42) == "42s"

    def test_minutes_only(self):
        assert mod._format_duration(120) == "2m"

    def test_minutes_and_seconds(self):
        assert mod._format_duration(125) == "2m 5s"

    def test_hours_only(self):
        assert mod._format_duration(3600) == "1h"

    def test_hours_and_minutes(self):
        assert mod._format_duration(4500) == "1h 15m"


# ── one-session task ──────────────────────────────────────────────────


class TestOneSessionTask:
    """Normal merge path: one session, cost, diff ignored (no repo), criteria present."""

    def test_summary_fields_populated(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(
            conn, task_id=1, summary="Ship feature X",
            started_at="2026-04-19 10:00:00",
            closed_at="2026-04-19 12:30:00",
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) "
            "VALUES (?, ?, ?, ?)",
            (1, "2026-04-19 10:00:00", "2026-04-19 12:30:00", 9000),
        )
        conn.execute(
            "INSERT INTO skill_runs (skill_name, task_id, cost_dollars) VALUES ('tusk', 1, 0.5432)"
        )
        conn.executemany(
            "INSERT INTO acceptance_criteria (task_id, criterion, criterion_type) VALUES (?, ?, ?)",
            [(1, "A", "manual"), (1, "B", "test"), (1, "C", "file")],
        )
        conn.execute(
            "INSERT INTO code_reviews (task_id, status) VALUES (1, 'approved')"
        )
        conn.commit()

        # repo_root pointed at tmp_path — no git history → diff is all zeros
        data = mod.build_summary(conn, 1, str(tmp_path))
        assert data["task_id"] == 1
        assert data["prefixed_id"] == "TASK-1"
        assert data["status"] == "Done"
        assert data["closed_reason"] == "completed"
        assert data["cost"]["total"] == 0.5432
        assert data["cost"]["skill_run_count"] == 1
        assert data["duration"]["session_count"] == 1
        assert data["duration"]["active_seconds"] == 9000
        assert data["duration"]["wall_seconds"] == 9000  # 2h 30m
        assert data["criteria"]["total"] == 3
        assert data["criteria"]["manual"] == 1
        assert data["criteria"]["automated"] == 2
        assert data["review_passes"] == 1
        assert data["reopen_count"] == 0


# ── multi-session task ────────────────────────────────────────────────


class TestMultiSessionTask:
    """Earliest session.started_at drives wall; SUM(duration_seconds) drives active."""

    def test_wall_from_first_start(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(
            conn, task_id=2, summary="Long running task",
            started_at="2026-04-18 09:00:00",
            closed_at="2026-04-19 17:00:00",   # +32h wall
        )
        # Three sessions — the earliest is session 1 at 2026-04-18 09:00.
        # Active time sums to 3h regardless of wall gap.
        conn.executemany(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) "
            "VALUES (?, ?, ?, ?)",
            [
                (2, "2026-04-18 09:00:00", "2026-04-18 10:00:00", 3600),
                (2, "2026-04-19 08:00:00", "2026-04-19 09:30:00", 5400),
                (2, "2026-04-19 16:00:00", "2026-04-19 17:00:00", 3600),
            ],
        )
        conn.executemany(
            "INSERT INTO skill_runs (skill_name, task_id, cost_dollars) VALUES (?, ?, ?)",
            [("tusk", 2, 1.0), ("retro", 2, 0.25), ("review-commits", 2, 0.5)],
        )
        conn.commit()

        data = mod.build_summary(conn, 2, str(tmp_path))
        assert data["duration"]["session_count"] == 3
        assert data["duration"]["active_seconds"] == 3600 + 5400 + 3600
        assert data["duration"]["wall_seconds"] == 32 * 3600
        assert data["cost"]["total"] == 1.75
        assert data["cost"]["skill_run_count"] == 3


# ── zero-commit abandon path ──────────────────────────────────────────


class TestAbandonPath:
    """wont_do closure with no code shipped: diff is all zeros, criteria may be skipped."""

    def test_abandon_zero_diff(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(
            conn, task_id=3, summary="Spike we bailed on",
            closed_reason="wont_do",
            started_at="2026-04-19 10:00:00",
            closed_at="2026-04-19 10:45:00",
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) "
            "VALUES (?, ?, ?, ?)",
            (3, "2026-04-19 10:00:00", "2026-04-19 10:45:00", 2700),
        )
        conn.executemany(
            "INSERT INTO acceptance_criteria (task_id, criterion, criterion_type, is_deferred, skip_note) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (3, "Investigate X", "manual", 0, "Proved not worth implementing"),
                (3, "Prototype Y", "manual", 1, None),  # deferred via criteria skip
            ],
        )
        conn.commit()

        data = mod.build_summary(conn, 3, str(tmp_path))
        assert data["closed_reason"] == "wont_do"
        assert data["diff"] == {
            "commits": 0,
            "files_changed": 0,
            "lines_added": 0,
            "lines_removed": 0,
        }
        assert data["criteria"]["skip_notes"] == 1
        assert data["criteria"]["deferred"] == 1


# ── reopen_count > 0 ──────────────────────────────────────────────────


class TestReopenCount:
    def test_counts_transitions_back_to_todo(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(conn, task_id=4, summary="Reopened task")
        conn.executemany(
            "INSERT INTO task_status_transitions (task_id, from_status, to_status) VALUES (?, ?, ?)",
            [
                (4, "In Progress", "To Do"),   # mid-task rework
                (4, "Done", "To Do"),          # post-done reopen
                (4, "In Progress", "Done"),    # not a reopen
                (5, "In Progress", "To Do"),   # different task — ignored
            ],
        )
        conn.commit()
        data = mod.build_summary(conn, 4, str(tmp_path))
        assert data["reopen_count"] == 2


# ── diff parsing (git subprocess integration) ──────────────────────────


class TestDiff:
    """Exercise fetch_diff against a real tmp git repo — covers the numstat
    parser, the [TASK-<id>] filter, and exclusion of unrelated commits."""

    def _init_repo(self, repo_root):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=repo_root, check=True
        )

    def _commit(self, repo_root, path, content, message, commit_date=None):
        full = os.path.join(repo_root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(path) else None
        with open(full, "w") as f:
            f.write(content)
        subprocess.run(["git", "add", path], cwd=repo_root, check=True)
        env = os.environ.copy()
        if commit_date:
            env["GIT_AUTHOR_DATE"] = commit_date
            env["GIT_COMMITTER_DATE"] = commit_date
        subprocess.run(
            ["git", "commit", "-q", "-m", message], cwd=repo_root, check=True, env=env
        )

    def test_diff_filters_to_task(self, tmp_path):
        repo = str(tmp_path)
        self._init_repo(repo)
        self._commit(repo, "a.txt", "one\n", "[TASK-7] first")
        self._commit(repo, "a.txt", "one\ntwo\n", "[TASK-7] second")
        self._commit(repo, "b.txt", "x\n", "[TASK-8] unrelated")  # different task — excluded

        diff = mod.fetch_diff(7, repo)
        assert diff["commits"] == 2
        assert diff["files_changed"] == 1       # only a.txt
        assert diff["lines_added"] == 2         # 1 line each commit
        assert diff["lines_removed"] == 0

    def test_diff_empty_when_no_matching_commits(self, tmp_path):
        repo = str(tmp_path)
        self._init_repo(repo)
        self._commit(repo, "a.txt", "x\n", "no task reference")
        diff = mod.fetch_diff(42, repo)
        assert diff == {
            "commits": 0,
            "files_changed": 0,
            "lines_added": 0,
            "lines_removed": 0,
        }

    def test_diff_gracefully_handles_missing_repo(self, tmp_path):
        """fetch_diff against a non-git dir returns all zeros (no exception)."""
        diff = mod.fetch_diff(1, str(tmp_path))
        assert diff["commits"] == 0

    def test_diff_excludes_commits_before_started_at(self, tmp_path):
        """Two [TASK-7] commits sharing a numeric ID across DB lifetimes:
        only the one authored after `since` is counted."""
        repo = str(tmp_path)
        self._init_repo(repo)
        # Earlier incarnation of TASK-7 — pre-dates the current task's started_at.
        self._commit(
            repo, "old.txt", "old\n", "[TASK-7] earlier incarnation",
            commit_date="2026-01-15 10:00:00 +0000",
        )
        # Current TASK-7 commits — after started_at.
        self._commit(
            repo, "new.txt", "one\n", "[TASK-7] current — first",
            commit_date="2026-04-19 11:00:00 +0000",
        )
        self._commit(
            repo, "new.txt", "one\ntwo\n", "[TASK-7] current — second",
            commit_date="2026-04-19 12:00:00 +0000",
        )

        # Without `since`, all three [TASK-7] commits leak in.
        unscoped = mod.fetch_diff(7, repo)
        assert unscoped["commits"] == 3
        assert unscoped["files_changed"] == 2

        # With `since=started_at`, the earlier incarnation is excluded.
        scoped = mod.fetch_diff(7, repo, since="2026-04-19 10:00:00")
        assert scoped["commits"] == 2
        assert scoped["files_changed"] == 1
        assert scoped["lines_added"] == 2
        assert scoped["lines_removed"] == 0


# ── end-to-end: CLI exit codes and output modes ───────────────────────


class TestCli:
    def test_not_found_exits_nonzero(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.close()
        rc, out, err = _run_main(db_path, 999)
        assert rc == 1
        assert "not found" in err.lower()

    def test_json_output(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(conn, task_id=11)
        conn.close()
        rc, out, err = _run_main(db_path, 11, fmt="json")
        assert rc == 0, err
        payload = json.loads(out)
        assert payload["task_id"] == 11
        assert payload["prefixed_id"] == "TASK-11"

    def test_markdown_output_header(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(conn, task_id=11, summary="Short task")
        conn.close()
        rc, out, err = _run_main(db_path, 11, fmt="markdown")
        assert rc == 0, err
        assert out.startswith("## TASK-11 — Short task")

    def test_accepts_task_prefix_form(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(conn, task_id=77)
        conn.close()
        rc, out, err = _run_main(db_path, "TASK-77")
        assert rc == 0, err


class TestRenderMarkdown:
    """Quick checks that the rendered block reflects the data, with clean
    singular/plural agreement for the common shapes."""

    def _sample(self, **overrides):
        base = {
            "task_id": 1,
            "prefixed_id": "TASK-1",
            "summary": "Test",
            "status": "Done",
            "closed_reason": "completed",
            "cost": {"total": 0.0, "skill_run_count": 1},
            "duration": {"wall_seconds": 60, "active_seconds": 30, "session_count": 1,
                         "started_at": None, "closed_at": None},
            "diff": {"commits": 1, "files_changed": 1, "lines_added": 5, "lines_removed": 2},
            "criteria": {"total": 1, "manual": 1, "automated": 0, "skip_notes": 0, "deferred": 0},
            "review_passes": 0,
            "reopen_count": 0,
        }
        base.update(overrides)
        return base

    def test_singular_forms(self):
        out = mod.render_markdown(self._sample())
        assert "1 skill run" in out and "1 skill runs" not in out
        assert "1 session" in out and "1 sessions" not in out
        assert "1 file" in out and "1 files" not in out
        assert "1 commit" in out and "1 commits" not in out

    def test_plural_forms(self):
        out = mod.render_markdown(self._sample(
            cost={"total": 0.0, "skill_run_count": 3},
            duration={"wall_seconds": 60, "active_seconds": 30, "session_count": 2,
                      "started_at": None, "closed_at": None},
            diff={"commits": 4, "files_changed": 5, "lines_added": 0, "lines_removed": 0},
        ))
        assert "3 skill runs" in out
        assert "2 sessions" in out
        assert "5 files" in out
        assert "4 commits" in out

    def test_reopen_badge_appears_only_when_nonzero(self):
        out_zero = mod.render_markdown(self._sample(reopen_count=0))
        assert "Reopened" not in out_zero
        out_nonzero = mod.render_markdown(self._sample(reopen_count=3))
        assert "Reopened:** 3×" in out_nonzero

    def test_skip_and_deferred_counters_appear_only_when_nonzero(self):
        out_zero = mod.render_markdown(self._sample())
        assert "skip-verify" not in out_zero
        assert "deferred" not in out_zero
        out_with = mod.render_markdown(self._sample(
            criteria={"total": 3, "manual": 3, "automated": 0, "skip_notes": 1, "deferred": 2},
        ))
        assert "1 skip-verify" in out_with
        assert "2 deferred" in out_with

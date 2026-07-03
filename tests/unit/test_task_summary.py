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
    description TEXT,
    status TEXT DEFAULT 'To Do',
    closed_reason TEXT,
    complexity TEXT,
    started_at TEXT,
    closed_at TEXT
);
CREATE TABLE task_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds INTEGER,
    cost_dollars REAL
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
    verification_spec TEXT,
    is_completed INTEGER DEFAULT 0,
    is_deferred INTEGER DEFAULT 0,
    deferred_reason TEXT,
    skip_note TEXT,
    commit_hash TEXT
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
    started_at TEXT,
    ended_at TEXT,
    cost_dollars REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    request_count INTEGER
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
                 closed_reason="completed", complexity=None,
                 started_at=None, closed_at=None):
    conn.execute(
        "INSERT INTO tasks (id, summary, status, closed_reason, complexity, started_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, summary, status, closed_reason, complexity, started_at, closed_at),
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

    def test_summary_includes_completed_unattributed_skill_run_in_task_session(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(
            conn, task_id=12, summary="Cost attribution fallback",
            complexity="S",
            started_at="2026-04-19 10:00:00",
            closed_at="2026-04-19 12:30:00",
        )
        _insert_task(
            conn, task_id=13, summary="Peer with fallback cost",
            complexity="S",
            started_at="2026-04-18 10:00:00",
            closed_at="2026-04-18 12:30:00",
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) "
            "VALUES (?, ?, ?, ?)",
            (12, "2026-04-19 10:00:00", "2026-04-19 12:30:00", 9000),
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds) "
            "VALUES (?, ?, ?, ?)",
            (13, "2026-04-18 10:00:00", "2026-04-18 12:30:00", 9000),
        )
        conn.execute(
            "INSERT INTO skill_runs "
            "(skill_name, task_id, started_at, ended_at, cost_dollars, tokens_in, tokens_out, request_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "tusk",
                None,
                "2026-04-19 10:15:00",
                "2026-04-19 12:20:00",
                0.1178,
                12000,
                3000,
                8,
            ),
        )
        conn.execute(
            "INSERT INTO skill_runs "
            "(skill_name, task_id, started_at, ended_at, cost_dollars, tokens_in, tokens_out, request_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "tusk",
                None,
                "2026-04-18 10:15:00",
                "2026-04-18 12:20:00",
                0.2,
                10000,
                2000,
                6,
            ),
        )
        conn.commit()

        data = mod.build_summary(conn, 12, str(tmp_path), baseline_threshold=1)

        assert data["cost"] == {"total": 0.1178, "skill_run_count": 1}
        assert data["baseline_comparison"] == {
            "bucket": "S",
            "median_cost": 0.2,
            "n": 1,
            "ratio": 0.59,
            "threshold": 1,
            "status": "compared",
        }
        assert data["tokens"] == {
            "tokens_in": 12000,
            "tokens_out": 3000,
            "request_count": 8,
        }
        markdown = mod.render_markdown(data)
        assert "- **Cost:** $0.1178 across 1 skill run" in markdown
        assert "/bin/zsh.0000" not in markdown

    def test_summary_includes_main_session_cost_and_extra_skill_runs(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(
            conn, task_id=14, summary="Main plus review cost",
            complexity="M",
            started_at="2026-04-19 10:00:00",
            closed_at="2026-04-19 12:30:00",
        )
        _insert_task(
            conn, task_id=15, summary="Peer main plus review cost",
            complexity="M",
            started_at="2026-04-18 10:00:00",
            closed_at="2026-04-18 12:30:00",
        )
        conn.execute(
            "INSERT INTO task_sessions "
            "(task_id, started_at, ended_at, duration_seconds, cost_dollars) "
            "VALUES (?, ?, ?, ?, ?)",
            (14, "2026-04-19 10:00:00", "2026-04-19 12:30:00", 9000, 0.9508),
        )
        conn.execute(
            "INSERT INTO task_sessions "
            "(task_id, started_at, ended_at, duration_seconds, cost_dollars) "
            "VALUES (?, ?, ?, ?, ?)",
            (15, "2026-04-18 10:00:00", "2026-04-18 12:30:00", 9000, 1.0),
        )
        conn.executemany(
            "INSERT INTO skill_runs "
            "(skill_name, task_id, started_at, ended_at, cost_dollars, tokens_in, tokens_out, request_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "tusk",
                    14,
                    "2026-04-19 10:00:05",
                    "2026-04-19 12:20:00",
                    0.9508,
                    12000,
                    3000,
                    8,
                ),
                (
                    "review-commits",
                    14,
                    "2026-04-19 12:20:00",
                    "2026-04-19 12:25:00",
                    0.7348,
                    4000,
                    1000,
                    3,
                ),
                (
                    "tusk",
                    None,
                    "2026-04-19 10:00:05",
                    "2026-04-19 12:20:00",
                    0.9508,
                    12000,
                    3000,
                    8,
                ),
                (
                    "review-commits",
                    15,
                    "2026-04-18 12:20:00",
                    "2026-04-18 12:25:00",
                    0.5,
                    3000,
                    1000,
                    2,
                ),
            ],
        )
        conn.commit()

        data = mod.build_summary(conn, 14, str(tmp_path), baseline_threshold=1)

        assert data["cost"] == {"total": 1.6856, "skill_run_count": 2}
        assert data["baseline_comparison"] == {
            "bucket": "M",
            "median_cost": 1.5,
            "n": 1,
            "ratio": 1.12,
            "threshold": 1,
            "status": "compared",
        }
        markdown = mod.render_markdown(data)
        assert "- **Cost:** $1.6856 across 2 skill runs" in markdown


# ── multi-session task ────────────────────────────────────────────────


class TestTokens:
    """build_summary returns a 'tokens' key aggregated across skill_runs."""

    def test_tokens_sum_across_skill_runs(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(conn, task_id=30)
        conn.executemany(
            "INSERT INTO skill_runs (skill_name, task_id, cost_dollars, "
            "tokens_in, tokens_out, request_count) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("tusk", 30, 0.1, 1000, 500, 10),
                ("retro", 30, 0.05, 200, 100, 3),
            ],
        )
        conn.commit()
        data = mod.build_summary(conn, 30, str(tmp_path))
        assert data["tokens"] == {
            "tokens_in": 1200,
            "tokens_out": 600,
            "request_count": 13,
        }

    def test_tokens_zero_when_no_skill_runs(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(conn, task_id=31)
        conn.commit()
        data = mod.build_summary(conn, 31, str(tmp_path))
        assert data["tokens"] == {
            "tokens_in": 0,
            "tokens_out": 0,
            "request_count": 0,
        }


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
            "recovered_via": None,
        }
        assert data["criteria"]["skip_notes"] == 1
        assert data["criteria"]["deferred"] == 1
        assert data["criteria"]["deferred_details"] == [
            {"id": data["criteria"]["deferred_details"][0]["id"],
             "criterion": "Prototype Y", "deferred_reason": None},
        ]


class TestDeferredDetails:
    """deferred_details surfaces id/criterion/deferred_reason for is_deferred=1 rows."""

    def test_includes_reason_when_set(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(conn, task_id=10, summary="Mutex criteria task")
        conn.executemany(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, criterion_type, is_completed, is_deferred, deferred_reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (10, "Apply rate limiting", "manual", 1, 0, None),
                (10, "Document why exempt", "manual", 0, 1,
                 "not applicable: chose rate-limiting branch"),
            ],
        )
        conn.commit()
        data = mod.build_summary(conn, 10, str(tmp_path))
        details = data["criteria"]["deferred_details"]
        assert len(details) == 1
        assert details[0]["criterion"] == "Document why exempt"
        assert details[0]["deferred_reason"] == "not applicable: chose rate-limiting branch"

    def test_empty_when_no_deferred(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert_task(conn, task_id=11, summary="No deferred")
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, criterion_type) "
            "VALUES (11, 'A', 'manual')"
        )
        conn.commit()
        data = mod.build_summary(conn, 11, str(tmp_path))
        assert data["criteria"]["deferred_details"] == []


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
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, encoding="utf-8"
        ).strip()

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
            "recovered_via": None,
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

    def test_diff_ignores_stale_criterion_hash_after_rebase(self, tmp_path):
        """Issue #711: criteria may retain pre-rebase SHAs after merge
        finalizes a rewritten [TASK-N] commit on the default branch. Diff
        stats must come from discoverable task commits, not criterion hashes.
        """
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)
        self._commit(repo, "migrations/2043.sql", "CREATE TABLE x(id INTEGER);\n", "initial")
        self._commit(
            repo,
            "migrations/2043.sql",
            "CREATE TABLE x(id INTEGER);\nALTER TABLE x ADD COLUMN name TEXT;\n",
            "[TASK-2043] add migration",
        )

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, status, started_at) VALUES (?, ?, ?, ?)",
            (2043, "Add migration", "Done", "2026-05-08 00:00:00"),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (2043, "Migration is applied", 1, "f7bf9339"),
        )
        conn.commit()

        diff = mod.fetch_diff(2043, repo, since="2026-05-08 00:00:00", conn=conn)
        assert diff["commits"] == 1
        assert diff["files_changed"] == 1
        assert diff["lines_added"] == 1
        assert diff["lines_removed"] == 0

    def test_diff_falls_back_to_completed_criterion_hashes_when_rebased_commit_not_on_refs(
        self, tmp_path
    ):
        """Issue #735: after --rebase, the useful commit hash may differ from
        the pre-rebase criterion hash and not be found by the ref-scoped
        `git log --all --grep` scan in the summarizing checkout. If completed
        criteria point at the rewritten commit, use those hashes as a recovery
        source instead of reporting a zero diff.

        Issue #845 layered the fsck unreachable-object fallback after this
        criterion-hash path. The criterion-hash recovery is still the cheaper
        and more targeted route when the commit_hash is known to be valid, so
        gating the fsck path on ``conn is None`` (i.e. no criterion hashes to
        try first) keeps both fallbacks doing their distinct jobs.
        """
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)
        self._commit(repo, "README.md", "seed\n", "initial")
        subprocess.run(["git", "checkout", "-q", "-b", "rebased-task"], cwd=repo, check=True)
        rewritten_sha = self._commit(
            repo,
            "bin/tusk-task-summary.py",
            "one\n",
            "[TASK-735] preserve rebased summary stats",
        )
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        subprocess.run(["git", "branch", "-D", "rebased-task"], cwd=repo, check=True)

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, status, started_at) VALUES (?, ?, ?, ?)",
            (735, "Fix bin/tusk-task-summary.py rebase summary stats", "Done", None),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (735, "Summary diff stats survive rebase", 1, rewritten_sha),
        )
        conn.commit()

        # With the issue #845 fsck fallback, fetch_diff is now strictly
        # more robust — even without a conn it can recover the unreachable
        # commit. This is a strict improvement over the prior "no conn =>
        # zero diff" behavior the test originally asserted.
        diff_no_conn = mod.fetch_diff(735, repo)
        assert diff_no_conn["commits"] == 1
        assert diff_no_conn["files_changed"] == 1

        diff = mod.fetch_diff(735, repo, conn=conn)
        assert diff["commits"] == 1
        assert diff["files_changed"] == 1
        assert diff["lines_added"] == 1
        assert diff["lines_removed"] == 0

    def test_criterion_hash_recovery_includes_ancestor_task_commits(self, tmp_path):
        """Issue #917: a later skip-verify criterion may record only the tip
        commit from a task branch. When that branch is no longer on any ref,
        criterion-hash recovery must walk back through contiguous [TASK-N]
        ancestors instead of reporting only the recorded tip commit.
        """
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)
        self._commit(repo, "README.md", "seed\n", "initial")

        subprocess.run(["git", "checkout", "-q", "-b", "manual-task"], cwd=repo, check=True)
        self._commit(
            repo,
            "schema.prisma",
            "model User { id String }\n",
            "[TASK-2474] manual path-limited schema change",
        )
        tip_sha = self._commit(
            repo,
            "app/route.test.ts",
            "test('route', () => {})\n",
            "[TASK-2474] mark route behavior covered",
        )
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        subprocess.run(["git", "branch", "-D", "manual-task"], cwd=repo, check=True)

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, status, started_at) VALUES (?, ?, ?, ?)",
            (2474, "Fix route summary accounting", "Done", None),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash, skip_note) "
            "VALUES (?, ?, ?, ?, ?)",
            (2474, "Route behavior verified manually", 1, tip_sha, "skip-verify"),
        )
        conn.commit()

        diff = mod.fetch_diff(2474, repo, conn=conn)
        assert diff["recovered_via"] == "criterion-hash"
        assert diff["commits"] == 2
        assert diff["files_changed"] == 2
        assert diff["lines_added"] == 2
        assert diff["lines_removed"] == 0


class TestDiffPrefixCollisionHeuristic:
    """TASK-309 / issue #656: when a connection is provided and the task
    has a positive scope signal, drop commit blocks whose aggregate file
    diff doesn't overlap with this task's referenced paths so the end-of-run
    summary isn't inflated by a stray [TASK-N] match (recycled task ID,
    fat-fingered commit message authored after the task started).

    Issue #663 / TASK-324: the filter is applied **block-level** — commits
    contiguous in git history (parent-child via grep-matched commits) form
    one block; if any commit in the block touches a referenced path, the
    whole block is kept. This preserves legitimate sibling commits (VERSION
    bumps, CHANGELOG, new test files, brand-new feature files) whose paths
    aren't pre-named in the task text.
    """

    def _init_repo(self, repo_root):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=repo_root, check=True
        )

    def _commit(self, repo_root, path, content, message):
        full = os.path.join(repo_root, path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        subprocess.run(["git", "add", path], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", message], cwd=repo_root, check=True
        )

    def test_keeps_contiguous_block_with_one_overlap(self, tmp_path):
        """Issue #663: A contiguous block of [TASK-N] commits where only
        ONE commit touches a path named in task scope. Block-level filter
        keeps the whole block; per-commit filter (old) would drop the
        siblings. Models TASK-323's reproduction: VERSION/CHANGELOG/test
        commits ride along on the back of the CLAUDE.md commit."""
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)
        # Four contiguous [TASK-7] commits, only one names bin/tusk-foo.py
        self._commit(repo, "bin/tusk-foo.py", "x\n", "[TASK-7] feature impl")
        self._commit(repo, "VERSION", "1\n", "[TASK-7] bump VERSION")
        self._commit(repo, "CHANGELOG.md", "x\n", "[TASK-7] CHANGELOG entry")
        self._commit(repo, "tests/test_foo.py", "x\n", "[TASK-7] regression test")

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, status) VALUES (?, ?, ?)",
            (7, "Wire bin/tusk-foo.py for foo handling", "Done"),
        )
        conn.commit()

        filtered = mod.fetch_diff(7, repo, conn=conn)
        # All four commits in the contiguous block are kept.
        assert filtered["commits"] == 4
        assert filtered["files_changed"] == 4
        assert filtered["lines_added"] == 4

    def test_drops_isolated_prefix_collision_block(self, tmp_path):
        """Genuine prefix collision: a [TASK-N] commit on a separate branch
        (no parent-child link to the legitimate work) whose files don't
        overlap task scope. Models TASK-323's 8f2d59f case: a recycled
        task ID's stale commit lingers in history. The block-level filter
        drops it because its standalone block has no scope-signal overlap."""
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)

        # Seed main with a non-task commit so the branch ref exists before we fork.
        self._commit(repo, "README.md", "seed\n", "initial")

        # Stray prefix-collision commit on its own orphan branch (no parent
        # link to the legitimate work on main, so it lands in its own block).
        subprocess.run(
            ["git", "checkout", "-q", "--orphan", "stale-prefix"],
            cwd=repo, check=True,
        )
        subprocess.run(["git", "rm", "-q", "-rf", "."], cwd=repo, check=True)
        self._commit(repo, "noise.txt", "x\n", "[TASK-7] recycled-id collision")
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

        # Legitimate work on main — separate block from the stale-prefix branch
        self._commit(repo, "bin/tusk-foo.py", "x\n", "[TASK-7] real")

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, status) VALUES (?, ?, ?)",
            (7, "Wire bin/tusk-foo.py for foo handling", "Done"),
        )
        conn.commit()

        # Without conn → both blocks leak in
        unfiltered = mod.fetch_diff(7, repo)
        assert unfiltered["commits"] == 2

        # With conn → isolated stray block drops; legitimate block kept
        filtered = mod.fetch_diff(7, repo, conn=conn)
        assert filtered["commits"] == 1
        assert filtered["files_changed"] == 1
        assert filtered["lines_added"] == 1
        # Confirm the surviving file is the real one, not the noise file
        # (block grouping kept the right block).
        assert filtered["lines_removed"] == 0

    def test_kept_when_no_scope_signal(self, tmp_path):
        """Task with no referenced paths in summary/description/criteria →
        heuristic has no basis to filter, so every commit is kept."""
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)
        self._commit(repo, "anything.txt", "x\n", "[TASK-9] generic")

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, status) VALUES (?, ?, ?)",
            (9, "Generic title with no paths", "Done"),
        )
        conn.commit()

        filtered = mod.fetch_diff(9, repo, conn=conn)
        assert filtered["commits"] == 1
        assert filtered["files_changed"] == 1


class TestBareBasenameMatching:
    """Issue #670: a description that names the touched file by bare
    basename (e.g. ``FULL-RETRO.md``) while also naming a sibling by
    full path (``skills/retro/SKILL.md``) used to drop every block in
    the strict full-path filter — the [TASK-N] commits all touched the
    bare-basename file plus VERSION/CHANGELOG, none of which intersected
    the single full-path token. The bare-basename helper resolves these
    via basename match, so legitimate sibling commits ride along.
    """

    def _init_repo(self, repo_root):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=repo_root, check=True
        )

    def _commit(self, repo_root, path, content, message):
        full = os.path.join(repo_root, path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        subprocess.run(["git", "add", path], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", message], cwd=repo_root, check=True
        )

    def test_extract_referenced_basenames_picks_up_bare_basenames(self):
        helpers = mod._git_helpers
        text = "Mirror the gate from skills/retro/SKILL.md into FULL-RETRO.md"
        names = helpers.extract_referenced_basenames(text)
        # SKILL.md is part of skills/retro/SKILL.md (full-path match) so it's
        # filtered out. FULL-RETRO.md has no directory prefix → bare candidate.
        assert "FULL-RETRO.md" in names
        assert "SKILL.md" not in names

    def test_extract_referenced_basenames_skips_whitelisted_bare(self):
        # CLAUDE.md / VERSION / CHANGELOG.md are already extracted by
        # extract_paths via the bare-toplevel whitelist; the basename
        # helper must not duplicate them.
        helpers = mod._git_helpers
        names = helpers.extract_referenced_basenames(
            "See CLAUDE.md and CHANGELOG.md and VERSION."
        )
        assert names == []

    def test_extract_referenced_basenames_ignores_short_extension_words(self):
        # "e.g." and "i.e." would otherwise be classified as basenames if
        # the regex allowed single-char extensions; reject them.
        helpers = mod._git_helpers
        names = helpers.extract_referenced_basenames(
            "see e.g. step 5 (i.e. the gate logic)"
        )
        assert names == []

    def test_extract_referenced_basenames_ignores_url_components(self):
        helpers = mod._git_helpers
        names = helpers.extract_referenced_basenames(
            "https://github.com/foo/bar/issues/670 — also see schema.md"
        )
        # github.com sits behind '//' — the leading-boundary regex can
        # only start a match after whitespace/quotes/parens/commas, not
        # '/', so URL fragments don't leak in.
        assert "github.com" not in names
        assert "schema.md" in names

    def test_block_kept_via_bare_basename_match(self, tmp_path):
        """Concrete TASK-330 reproduction (issue #670): description names
        skills/retro/SKILL.md (full path) AND FULL-RETRO.md (bare). The
        commit set touches FULL-RETRO.md and VERSION but never SKILL.md
        — old strict filter dropped the whole block; basename match keeps
        it and VERSION rides along on block contiguity.
        """
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)
        self._commit(
            repo, "skills/retro/FULL-RETRO.md", "x\n",
            "[TASK-30] Mirror gate into FULL-RETRO.md"
        )
        self._commit(repo, "VERSION", "1\n", "[TASK-30] bump VERSION")

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, description, status) VALUES (?, ?, ?, ?)",
            (
                30,
                "Mirror gate logic into FULL-RETRO.md",
                "Mirror skills/retro/SKILL.md gate logic into FULL-RETRO.md.",
                "Done",
            ),
        )
        conn.commit()

        filtered = mod.fetch_diff(30, repo, conn=conn)
        assert filtered["commits"] == 2
        assert filtered["files_changed"] == 2

    def test_off_scope_citation_keeps_real_work(self, tmp_path):
        """Issue #851 / TASK-433 / original TASK-430 reproduction: the task
        description mentions a file as a precedent citation
        ("matching the precedent set by ... (CLAUDE.md section)") rather than
        as the work subject. Path extraction picks up CLAUDE.md as a
        referenced path; the actual commits touch bin/tusk-task-summary.py
        and friends, none touch CLAUDE.md.

        Pre-#851: filter dropped every block, summary reported 0/0/0/0 for
        every shipped commit. Post-#851: extraction-miss fall-through keeps
        all blocks so the summary at least reflects the real diff.
        """
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)

        # Three contiguous [TASK-430] commits touching the real subject —
        # none touch CLAUDE.md (the parenthetical citation).
        self._commit(repo, "bin/tusk-task-summary.py", "x\n",
                     "[TASK-430] surface recovery tier")
        self._commit(repo, "tests/integration/test_recovery.py", "x\n",
                     "[TASK-430] regression coverage")
        self._commit(repo, "VERSION", "954\n", "[TASK-430] bump VERSION")

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, description, status) "
            "VALUES (?, ?, ?, ?)",
            (
                430,
                "Surface recovery tier via stderr",
                "Surface the recovery tier (matching the precedent set by "
                "bin/tusk active-projects drift warning "
                "(CLAUDE.md Cross-repo CWD pinning section)).",
                "Done",
            ),
        )
        conn.commit()

        filtered = mod.fetch_diff(430, repo, conn=conn)
        # All three real commits surface — extraction-miss fall-through is
        # the right call here even though scope signal CLAUDE.md exists.
        assert filtered["commits"] == 3
        assert filtered["files_changed"] == 3

    def test_extraction_miss_falls_through_to_keep_all(self, tmp_path):
        """Issue #851 / TASK-433: when scope signal exists but NO block in the
        candidate set overlaps it, the policy is to keep every commit rather
        than drop them all. The off-scope-citation case (description name-checks
        an unrelated file like CLAUDE.md as a precedent) is far more common
        than the all-stray-collision case (every [TASK-N] commit in the session
        window is a recycled-ID accident). Silent zero-stats is worse than a
        small false-positive surface — the latter is recoverable on inspection.

        Pre-#851: this scenario asserted filtered["commits"] == 0 (drop). The
        policy was flipped because TASK-430 (and similar) reported zero diff
        stats for completed tasks whose description happened to cite an
        off-scope path while the commits touched the real subject.
        """
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._init_repo(repo)
        # Seed main so the orphan-fork branching has a parent ref.
        self._commit(repo, "README.md", "seed\n", "initial")
        # Orphan branch keeps the prefix-collision commit in its own block.
        subprocess.run(
            ["git", "checkout", "-q", "--orphan", "stale"],
            cwd=repo, check=True,
        )
        subprocess.run(["git", "rm", "-q", "-rf", "."], cwd=repo, check=True)
        self._commit(repo, "unrelated.txt", "x\n", "[TASK-30] stale prefix")
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

        db_path, conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, description, status) VALUES (?, ?, ?, ?)",
            (
                30,
                "Edit FULL-RETRO.md only",
                "Edit FULL-RETRO.md only.",
                "Done",
            ),
        )
        conn.commit()

        # The bare-basename FULL-RETRO.md does not match unrelated.txt's
        # basename — under the issue #851 extraction-miss fall-through, the
        # filter returns every grep-matched commit unchanged.
        filtered = mod.fetch_diff(30, repo, conn=conn)
        assert filtered["commits"] == 1
        assert filtered["files_changed"] == 1


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
            "baseline_comparison": {
                "bucket": None, "median_cost": None, "n": 0, "ratio": None,
                "threshold": 10, "status": "no_complexity",
            },
            "duration": {"wall_seconds": 60, "active_seconds": 30, "session_count": 1,
                         "started_at": None, "closed_at": None},
            "diff": {"commits": 1, "files_changed": 1, "lines_added": 5, "lines_removed": 2},
            "criteria": {"total": 1, "manual": 1, "automated": 0, "skip_notes": 0, "deferred": 0, "deferred_details": []},
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
            criteria={"total": 3, "manual": 3, "automated": 0, "skip_notes": 1, "deferred": 2,
                      "deferred_details": []},
        ))
        assert "1 skip-verify" in out_with
        assert "2 deferred" in out_with

    def test_deferred_details_render_as_sublist(self):
        out = mod.render_markdown(self._sample(
            criteria={"total": 2, "manual": 2, "automated": 0, "skip_notes": 0, "deferred": 1,
                      "deferred_details": [
                          {"id": 42, "criterion": "Document why exempt",
                           "deferred_reason": "not applicable: chose rate-limiting"},
                      ]},
        ))
        assert "1 deferred" in out
        assert "_Deferred #42 (not applicable: chose rate-limiting):_ Document why exempt" in out

    def test_deferred_details_handle_null_reason(self):
        out = mod.render_markdown(self._sample(
            criteria={"total": 1, "manual": 1, "automated": 0, "skip_notes": 0, "deferred": 1,
                      "deferred_details": [
                          {"id": 7, "criterion": "Legacy criterion", "deferred_reason": None},
                      ]},
        ))
        assert "_Deferred #7 (no reason given):_ Legacy criterion" in out

    def test_baseline_compared_renders_multiplier_and_bucket(self):
        out = mod.render_markdown(self._sample(
            cost={"total": 0.30, "skill_run_count": 1},
            baseline_comparison={
                "bucket": "M", "median_cost": 0.20, "n": 12, "ratio": 1.5,
                "threshold": 10, "status": "compared",
            },
        ))
        assert "1.5x baseline" in out
        assert "M median: $0.2000" in out
        assert "n=12" in out

    def test_baseline_compared_zero_cost_suppresses_multiplier(self):
        out = mod.render_markdown(self._sample(
            cost={"total": 0.0, "skill_run_count": 1},
            baseline_comparison={
                "bucket": "M", "median_cost": 0.20, "n": 12, "ratio": None,
                "threshold": 10, "status": "compared",
            },
        ))
        assert "x baseline" not in out  # no multiplier in either form
        assert "(M median: $0.2000, n=12)" in out

    def test_baseline_pending_renders_M_over_N(self):
        out = mod.render_markdown(self._sample(
            cost={"total": 0.30, "skill_run_count": 1},
            baseline_comparison={
                "bucket": "L", "median_cost": 0.15, "n": 4, "ratio": None,
                "threshold": 10, "status": "pending",
            },
        ))
        assert "baseline pending" in out
        assert "L bucket has 4/10 closed tasks" in out
        assert "x baseline" not in out

    def test_baseline_no_peers_renders_zero_over_N(self):
        out = mod.render_markdown(self._sample(
            cost={"total": 1.00, "skill_run_count": 1},
            baseline_comparison={
                "bucket": "XL", "median_cost": None, "n": 0, "ratio": None,
                "threshold": 10, "status": "no_peers",
            },
        ))
        assert "XL bucket has 0/10 closed tasks" in out

    def test_recovered_via_note_omitted_when_null(self):
        """Cheap-path diff (recovered_via=None) must not surface the recovery
        note — the line is conditional so the common case stays uncluttered."""
        out = mod.render_markdown(self._sample(
            diff={"commits": 1, "files_changed": 1, "lines_added": 5,
                  "lines_removed": 2, "recovered_via": None},
        ))
        assert "recovered via" not in out

    def test_recovered_via_note_omitted_when_field_missing(self):
        """Backwards compatibility: a diff dict without the recovered_via key
        (e.g. legacy callers) must not surface the note either."""
        out = mod.render_markdown(self._sample(
            diff={"commits": 1, "files_changed": 1, "lines_added": 5,
                  "lines_removed": 2},
        ))
        assert "recovered via" not in out

    def test_recovered_via_note_renders_when_tier_set(self):
        """Issue #852: when recovered_via is set, a one-line note appears
        between the Criteria line and the deferred details — giving operators
        a visible signal that stats came from a fallback tier."""
        for tier in ("refresh-fetch", "criterion-hash", "fsck-unreachable"):
            out = mod.render_markdown(self._sample(
                diff={"commits": 1, "files_changed": 1, "lines_added": 5,
                      "lines_removed": 2, "recovered_via": tier},
            ))
            assert f"recovered via `{tier}` tier" in out, (
                f"Expected recovered_via note for tier {tier!r}; got: {out!r}"
            )

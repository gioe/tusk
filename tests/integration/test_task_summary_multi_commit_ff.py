"""Regression test for TASK-452 (follow-up to TASK-451, issue #849).

Migration 70's stamped-sha fast-path in ``bin/tusk-task-summary.py`` ran
``git show --numstat <merge_commit_sha>`` against a single stamped SHA.
For PR squash merges (one squash commit holds all task work) that is
correct, but for **fast-forward** and **no-checkout fast-forward push**
merges the stamp is the *tip* of an N-commit feature branch, so
``git show`` reports only the last commit's numstat — understating
multi-commit task work as 1 commit / last-commit diff.

Migration 72 adds ``tasks.merge_base_sha`` so the fast-path can run
``git log --first-parent --numstat <base>..<tip>`` across the full task
range. PR squash callers keep ``merge_base_sha`` NULL (single-SHA mode
remains correct for one squash commit).

Tests below build a real git repo with an N-commit feature branch
fast-forwarded onto ``main``, stamp the DB exactly as ``tusk merge``
would, and assert:

1. **Range mode produces cumulative stats** — covers criterion 2074 (the
   primary fix) and criterion 2076 (fast-path output matches what the
   recovery chain would have produced from the same git history).
2. **Single-SHA mode (PR squash) is unchanged** — covers criterion 2075:
   stamping only ``merge_commit_sha`` (base NULL) returns the single
   tip commit's stats, identical to migration 70 behavior.
3. **Recovery-chain parity** — explicitly clears the stamp and asserts
   ``fetch_diff`` returns the same numbers via the cheap-scan path, so
   the new fast-path is a faithful shortcut for the canonical
   ``git log --all --grep`` computation.
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_task_summary",
    os.path.join(BIN, "tusk-task-summary.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _run(cmd, cwd, check=True):
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_repo(repo_root):
    _run(["git", "init", "-q", "-b", "main"], cwd=repo_root)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo_root)
    _run(["git", "config", "user.name", "Test"], cwd=repo_root)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=repo_root)


def _make_db_with_task(tmp_path, task_id, started_at="2026-05-22 00:00:00"):
    """Minimal schema slice that satisfies fetch_diff's reads.

    Pins ``merge_commit_sha`` and ``merge_base_sha`` columns so the
    fast-path's SELECT succeeds; commit_hash on criteria so the
    criterion-hash recovery tier is exercisable.
    """
    db_path = tmp_path / "tasks.db"
    schema = """
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        summary TEXT,
        description TEXT,
        status TEXT,
        closed_reason TEXT,
        complexity TEXT,
        started_at TEXT,
        closed_at TEXT,
        merge_commit_sha TEXT,
        merge_base_sha TEXT
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
    CREATE TABLE task_status_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        changed_at TEXT DEFAULT (datetime('now'))
    );
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO tasks (id, summary, status, started_at) VALUES (?, ?, ?, ?)",
        (task_id, "Test task", "Done", started_at),
    )
    conn.commit()
    return db_path, conn


@pytest.fixture()
def repo_with_multi_commit_ff_merge(tmp_path):
    """Build a repo with a 3-commit feature branch fast-forwarded onto main.

    Returns ``(repo_path, merge_base_sha, merge_tip_sha)``. The tip SHA is
    what ``tusk merge`` would stamp as ``merge_commit_sha``; the base is
    what migration 72's ``merge_base_sha`` captures pre-merge. All three
    feature commits carry the ``[TASK-99]`` prefix.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(str(repo))

    # Establish main with one commit so merge-base has something to anchor on.
    (repo / "README.md").write_text("init\n")
    _run(["git", "add", "README.md"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[INIT] initial"], cwd=str(repo))
    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()

    # Branch + 3 task commits with distinct numstat shapes.
    _run(["git", "checkout", "-q", "-b", "feature/TASK-99-multi"], cwd=str(repo))

    (repo / "a.txt").write_text("alpha\nbeta\n")
    _run(["git", "add", "a.txt"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[TASK-99] add a.txt"], cwd=str(repo))

    (repo / "b.txt").write_text("one\ntwo\nthree\n")
    _run(["git", "add", "b.txt"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[TASK-99] add b.txt"], cwd=str(repo))

    (repo / "a.txt").write_text("alpha\nbeta\ngamma\ndelta\n")
    _run(["git", "add", "a.txt"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[TASK-99] extend a.txt"], cwd=str(repo))

    # Fast-forward merge onto main (simulates the local-ff path).
    _run(["git", "checkout", "-q", "main"], cwd=str(repo))
    _run(["git", "merge", "-q", "--ff-only", "feature/TASK-99-multi"], cwd=str(repo))
    tip_sha = _run(["git", "rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()

    return str(repo), base_sha, tip_sha


@pytest.fixture()
def repo_with_reopened_task_lifecycles(tmp_path):
    """Build two task merge lifecycles separated by unrelated main work."""
    repo = tmp_path / "reopened-repo"
    repo.mkdir()
    _init_repo(str(repo))

    (repo / "README.md").write_text("init\n")
    _run(["git", "add", "README.md"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[INIT] initial"], cwd=str(repo))

    _run(["git", "checkout", "-q", "-b", "feature/TASK-99-first"], cwd=str(repo))
    (repo / "first.txt").write_text("one\ntwo\n")
    _run(["git", "add", "first.txt"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[TASK-99] first lifecycle"], cwd=str(repo))
    _run(["git", "checkout", "-q", "main"], cwd=str(repo))
    _run(["git", "merge", "-q", "--ff-only", "feature/TASK-99-first"], cwd=str(repo))

    (repo / "unrelated.txt").write_text("not\npart\nof\ntask\n")
    _run(["git", "add", "unrelated.txt"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "unrelated main work"], cwd=str(repo))
    second_base = _run(["git", "rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()

    _run(["git", "checkout", "-q", "-b", "feature/TASK-99-second"], cwd=str(repo))
    (repo / "second.txt").write_text("three\nfour\nfive\n")
    _run(["git", "add", "second.txt"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[TASK-99] second lifecycle"], cwd=str(repo))
    _run(["git", "checkout", "-q", "main"], cwd=str(repo))
    _run(["git", "merge", "-q", "--ff-only", "feature/TASK-99-second"], cwd=str(repo))
    final_tip = _run(["git", "rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()

    return str(repo), second_base, final_tip


class TestMultiCommitFastForwardFastPath:
    """The primary regression: range mode returns cumulative stats while
    single-SHA mode preserves migration 70's PR squash behavior."""

    def test_range_mode_returns_cumulative_stats_across_all_task_commits(
        self, repo_with_multi_commit_ff_merge, tmp_path
    ):
        """Criterion 2074: stamping both base + tip drives the fast-path
        through ``git log --first-parent --numstat <base>..<tip>``, which
        sums every commit on the feature branch — not just the tip."""
        repo, base, tip = repo_with_multi_commit_ff_merge
        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            conn.execute(
                "UPDATE tasks SET merge_commit_sha = ?, merge_base_sha = ? WHERE id = ?",
                (tip, base, 99),
            )
            conn.commit()
            diff = mod.fetch_diff(99, repo, conn=conn)
        finally:
            conn.close()

        # 3 commits, 2 distinct files (a.txt, b.txt), 2+3+2=7 lines added,
        # 0 removed. The tip-only path would have reported 1 commit / 1 file
        # / 2 lines added (just the "extend a.txt" commit).
        assert diff["commits"] == 3
        assert diff["files_changed"] == 2
        assert diff["lines_added"] == 7
        assert diff["lines_removed"] == 0
        assert diff["recovered_via"] == "stamped-sha"

    def test_single_sha_mode_preserves_pr_squash_behavior(
        self, repo_with_multi_commit_ff_merge, tmp_path
    ):
        """Criterion 2075: leaving merge_base_sha NULL (the PR squash path)
        falls through to the migration 70 ``git show --numstat <tip>``
        behavior unchanged — single commit, last-commit's numstat only.

        Reuses the same repo because the assertion is structural (range
        vs. single-SHA dispatch), not a separate PR-squash topology."""
        repo, _base, tip = repo_with_multi_commit_ff_merge
        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            conn.execute(
                "UPDATE tasks SET merge_commit_sha = ?, merge_base_sha = NULL "
                "WHERE id = ?",
                (tip, 99),
            )
            conn.commit()
            diff = mod.fetch_diff(99, repo, conn=conn)
        finally:
            conn.close()

        # tip commit ("extend a.txt") is +2/-0 on one file. Migration 70's
        # exact behavior — this is what regresses to wrong-for-ff stats but
        # is correct-by-design for PR squash.
        assert diff["commits"] == 1
        assert diff["files_changed"] == 1
        assert diff["lines_added"] == 2
        assert diff["lines_removed"] == 0
        assert diff["recovered_via"] == "stamped-sha"

    def test_range_mode_matches_recovery_chain_output(
        self, repo_with_multi_commit_ff_merge, tmp_path
    ):
        """Criterion 2076: the range-aware fast-path returns identical
        numbers to what the cheap ``git log --all --grep`` scan would have
        produced. Confirms the fast-path is a faithful shortcut, not a
        divergent path.

        Computes the recovery-chain answer by clearing the stamp so
        ``fetch_diff`` skips the fast-path entirely and walks the same
        scan it would have without migration 70+72."""
        repo, base, tip = repo_with_multi_commit_ff_merge
        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            # Fast-path stats (range mode).
            conn.execute(
                "UPDATE tasks SET merge_commit_sha = ?, merge_base_sha = ? WHERE id = ?",
                (tip, base, 99),
            )
            conn.commit()
            fast = mod.fetch_diff(99, repo, conn=conn)

            # Recovery-chain stats (no stamp; falls through to git log scan).
            conn.execute(
                "UPDATE tasks SET merge_commit_sha = NULL, merge_base_sha = NULL "
                "WHERE id = ?",
                (99,),
            )
            conn.commit()
            scan = mod.fetch_diff(99, repo, conn=conn)
        finally:
            conn.close()

        for key in ("commits", "files_changed", "lines_added", "lines_removed"):
            assert fast[key] == scan[key], (
                f"fast-path vs. scan disagree on {key}: "
                f"fast={fast[key]}, scan={scan[key]}"
            )
        # The two paths are intentionally tagged differently — fast = stamped-sha,
        # scan = None (initial scan succeeded without recovery). The numbers
        # parity is what criterion 2076 asks for.
        assert fast["recovered_via"] == "stamped-sha"
        assert scan["recovered_via"] is None


class TestReopenedTaskCumulativeDiff:
    def test_reopened_task_bypasses_latest_stamp_and_sums_all_task_commits(
        self, repo_with_reopened_task_lifecycles, tmp_path
    ):
        repo, latest_base, latest_tip = repo_with_reopened_task_lifecycles
        _db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            conn.execute(
                "UPDATE tasks SET merge_commit_sha = ?, merge_base_sha = ? WHERE id = ?",
                (latest_tip, latest_base, 99),
            )
            conn.execute(
                "INSERT INTO task_status_transitions "
                "(task_id, from_status, to_status) VALUES (?, 'Done', 'To Do')",
                (99,),
            )
            conn.commit()

            diff = mod.fetch_diff(99, repo, conn=conn)
        finally:
            conn.close()

        assert diff["commits"] == 2
        assert diff["files_changed"] == 2
        assert diff["lines_added"] == 5
        assert diff["lines_removed"] == 0
        assert diff["recovered_via"] is None

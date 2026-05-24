"""Regression test for the prefix-collision file-overlap heuristic in
tusk-task-done.py (TASK-309 / issue #656).

The auto-mark-criteria-on-close path at bin/tusk-task-done.py:131 calls
``_find_task_commits(task_id)`` to grab a commit hash to attribute open
criteria to before flipping the task to Done. If a stray ``[TASK-<id>]``
commit (e.g. recycled task ID after a fresh DB init, or a fat-fingered
commit message on another task) sits in git history, the unguarded path
would stamp this task's criteria with that other task's hash and silently
close the task as completed.

The heuristic mirrors the one already wired into ``tusk merge`` (TASK-308)
and ``tusk check-deliverables`` / ``tusk task-unstart``: drop matched
commits whose file diff doesn't intersect this task's referenced paths,
provided the task has a positive scope signal.
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_task_done", os.path.join(BIN, "tusk-task-done.py")
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── schema fixture ────────────────────────────────────────────────────
# Minimal subset of the real schema — only the columns this script and
# task_referenced_paths read. Not a mirror of bin/tusk; no schema-sync
# guard is needed (other unit tests follow the same pattern).

_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    description TEXT,
    status TEXT DEFAULT 'To Do',
    closed_reason TEXT
);
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT,
    verification_spec TEXT,
    is_completed INTEGER DEFAULT 0,
    is_deferred INTEGER DEFAULT 0
);
"""


def _make_repo(tmp_path):
    """Create a minimal git repo with one seed commit on main and an empty DB."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    seed = repo / "seed.txt"
    seed.write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)

    tusk_dir = repo / "tusk"
    tusk_dir.mkdir()
    db_path = tusk_dir / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return str(repo), str(db_path), conn


def _commit(repo_root, path, content, message):
    full = os.path.join(repo_root, path)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    subprocess.run(["git", "-C", repo_root, "add", path], check=True)
    subprocess.run(
        ["git", "-C", repo_root, "commit", "-q", "-m", message], check=True
    )
    sha = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    return sha


class TestFilterCommitsByTaskOverlap:
    """The helper underlying the auto-mark-criteria gate at line 131."""

    def test_extraction_miss_fallthrough_keeps_off_scope_solo_commit(self, tmp_path):
        """Extraction-miss fallthrough (issue #851, applied to task-done via #855):
        when the only [TASK-N] commit in the set touches no scope-signal path,
        the helper keeps it rather than dropping. Rationale: with a single
        off-scope commit the path extraction is more likely wrong (e.g. a
        precedent citation in the description) than the commit being a
        recycled-ID stray — over-inclusion is recoverable, silent zero-stats
        is not. The drop semantic is preserved for the multi-block layout
        below."""
        repo, _db_path, conn = _make_repo(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
            (99, "Wire bar", "Update bin/tusk-foo.py to handle bar"),
        )
        conn.commit()

        stray = _commit(
            repo, "unrelated/other.py", "x\n", "[TASK-99] unrelated"
        )

        kept, dropped = mod._filter_commits_by_task_overlap(99, [stray], conn, repo)

        assert kept == [stray]
        assert dropped == []

    def test_keeps_commit_whose_diff_overlaps_task_paths(self, tmp_path):
        repo, _db_path, conn = _make_repo(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
            (42, "Wire foo", "Update bin/tusk-foo.py and tests/unit/test_foo.py"),
        )
        conn.commit()

        sha = _commit(
            repo, "bin/tusk-foo.py", "x\n", "[TASK-42] real work"
        )

        kept, dropped = mod._filter_commits_by_task_overlap(42, [sha], conn, repo)

        assert kept == [sha]
        assert dropped == []

    def test_keeps_all_when_task_has_no_scope_signal(self, tmp_path):
        """Without referenced paths the heuristic has nothing to compare —
        every commit is kept, mirroring TASK-308's behavior in tusk-merge."""
        repo, _db_path, conn = _make_repo(tmp_path)
        # No paths anywhere in summary/description/criteria
        conn.execute(
            "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
            (7, "Generic title", "Generic body with no file references"),
        )
        conn.commit()

        sha = _commit(repo, "anything.txt", "x\n", "[TASK-7] generic")

        kept, dropped = mod._filter_commits_by_task_overlap(7, [sha], conn, repo)

        assert kept == [sha]
        assert dropped == []

    def test_keeps_contiguous_block_siblings(self, tmp_path):
        """Block-level + sibling ride-along (issue #842 / #855): when a
        real [TASK-N] commit on a scope-signal path and a stray [TASK-N]
        commit on an unrelated path are contiguous on the parent chain
        (no non-matched commit between them), they form one block. The
        block's aggregate files include the scope-signal path, so the
        entire block — including the off-scope sibling — is kept.

        This is the intentional new policy migrated from per-commit
        (TASK-308/309) to block-level (TASK-433/434, centralized in #855):
        sibling commits (VERSION bumps, CHANGELOG entries, new-file tests)
        ride along on the back of an in-block commit that names a
        referenced path. The recycled-ID drop case is preserved by the
        non-contiguous layout in the next test."""
        repo, _db_path, conn = _make_repo(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
            (5, "Wire baz", "Update bin/tusk-baz.py for baz handling"),
        )
        conn.commit()

        real = _commit(repo, "bin/tusk-baz.py", "x\n", "[TASK-5] real")
        stray = _commit(repo, "noise/foo.txt", "y\n", "[TASK-5] stray")

        kept, dropped = mod._filter_commits_by_task_overlap(
            5, [real, stray], conn, repo
        )

        assert set(kept) == {real, stray}
        assert dropped == []

    def test_drops_off_scope_block_under_non_contiguous_layout(self, tmp_path):
        """Genuine recycled-ID drop case (issue #855 / #856 regression
        vector): when a real [TASK-N] commit lands on main and a stray
        [TASK-N] commit lives on a side branch with no parent-link to the
        real commit, they form two separate blocks. The real block's
        files overlap task_paths and is kept; the stray block has no
        overlap with task scope and is dropped — fallthrough does NOT fire
        because the real block intersected."""
        repo, _db_path, conn = _make_repo(tmp_path)
        conn.execute(
            "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
            (5, "Wire baz", "Update bin/tusk-baz.py for baz handling"),
        )
        conn.commit()

        # Stray on a side branch (parent = seed, unrelated to main's head)
        subprocess.run(
            ["git", "-C", repo, "checkout", "-q", "-b", "side"],
            check=True,
        )
        stray = _commit(repo, "noise/foo.txt", "y\n", "[TASK-5] stray")

        # Real on main (parent = seed, no parent-link to stray)
        subprocess.run(
            ["git", "-C", repo, "checkout", "-q", "main"],
            check=True,
        )
        real = _commit(repo, "bin/tusk-baz.py", "x\n", "[TASK-5] real")

        kept, dropped = mod._filter_commits_by_task_overlap(
            5, [real, stray], conn, repo
        )

        assert kept == [real]
        assert dropped == [stray]


class TestFindTaskCommitsRoutesThroughHelper:
    """_find_task_commits delegates to find_task_commits — the centralized
    helper handles BRE escaping and the global grep-arg policy. Confirm by
    pointing the wrapper at a real repo and verifying it returns matches."""

    def test_returns_matching_commits(self, tmp_path):
        repo, _db_path, _conn = _make_repo(tmp_path)
        sha = _commit(repo, "x.txt", "x\n", "[TASK-7] hello")
        # No-match for a different ID
        _commit(repo, "y.txt", "y\n", "[TASK-8] other")

        # Cwd-independent: pass repo_root explicitly
        result = mod._find_task_commits(7, repo)

        assert result == [sha]

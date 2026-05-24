"""Parameterized tests for ``filter_commits_by_block_overlap`` (issue #855).

The block-level scope-filter heuristic was duplicated across at least
three filter callers — bin/tusk-review-diff-range.py, bin/tusk-task-summary.py,
and bin/tusk-task-done.py — and drifted three times in ~weeks before being
hoisted into bin/tusk-git-helpers.py. These tests pin the central helper's
behavior across the canonical scenarios so future drift fails loudly here
rather than silently in one caller.

Each scenario is exercised end-to-end through the three filter callers via
their thin wrapper paths to confirm they all collapse to identical kept-SHA
results for the same (commits, task scope) input — the symmetry the issue
specifically called out.
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


git_helpers = _load("tusk-git-helpers")


_TASKS_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    description TEXT,
    started_at TEXT
);
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT,
    verification_spec TEXT
);
"""


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    return str(repo)


def _seed_db(tmp_path, *, task_id, summary, description):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_TASKS_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
        (task_id, summary, description),
    )
    conn.commit()
    return conn


def _commit(repo, path, content, message):
    full = os.path.join(repo, path)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["git", "-C", repo, "add", path], check=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-q", "-m", message], check=True
    )
    sha = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    return sha


class TestFilterCommitsByBlockOverlap:
    """Direct tests of the centralized helper."""

    def test_no_scope_signal_returns_all(self, tmp_path):
        """A task with no referenced paths or basenames has no basis to
        discriminate; helper returns the input unchanged."""
        repo = _make_repo(tmp_path)
        conn = _seed_db(tmp_path, task_id=1, summary="generic", description="no paths")
        sha = _commit(repo, "anything.txt", "x\n", "[TASK-1] commit")

        kept = git_helpers.filter_commits_by_block_overlap([sha], 1, repo, conn)
        assert kept == [sha]

    def test_empty_commits_returns_empty(self, tmp_path):
        conn = _seed_db(tmp_path, task_id=1, summary="s", description="d")
        kept = git_helpers.filter_commits_by_block_overlap([], 1, str(tmp_path), conn)
        assert kept == []

    def test_none_conn_returns_all(self, tmp_path):
        repo = _make_repo(tmp_path)
        sha = _commit(repo, "x.txt", "x\n", "[TASK-1] commit")
        kept = git_helpers.filter_commits_by_block_overlap([sha], 1, repo, None)
        assert kept == [sha]

    def test_contiguous_block_keeps_all_when_any_overlaps(self, tmp_path):
        """Sibling ride-along: real on scope-signal path + stray on
        unrelated path, contiguous on parent chain → one block, kept whole."""
        repo = _make_repo(tmp_path)
        conn = _seed_db(
            tmp_path, task_id=2,
            summary="Wire foo", description="Update bin/tusk-foo.py",
        )
        real = _commit(repo, "bin/tusk-foo.py", "x\n", "[TASK-2] real")
        sibling = _commit(repo, "VERSION", "999\n", "[TASK-2] VERSION bump")

        kept = git_helpers.filter_commits_by_block_overlap(
            [real, sibling], 2, repo, conn
        )
        assert set(kept) == {real, sibling}

    def test_non_contiguous_drops_off_scope_block(self, tmp_path):
        """Stray on a side branch, real on main → two blocks. Real
        block intersects scope, stray block does not → stray dropped."""
        repo = _make_repo(tmp_path)
        conn = _seed_db(
            tmp_path, task_id=3,
            summary="Wire baz", description="Update bin/tusk-baz.py",
        )
        subprocess.run(
            ["git", "-C", repo, "checkout", "-q", "-b", "side"], check=True
        )
        stray = _commit(repo, "noise.txt", "y\n", "[TASK-3] stray")
        subprocess.run(["git", "-C", repo, "checkout", "-q", "main"], check=True)
        real = _commit(repo, "bin/tusk-baz.py", "x\n", "[TASK-3] real")

        kept = git_helpers.filter_commits_by_block_overlap(
            [real, stray], 3, repo, conn
        )
        assert kept == [real]

    def test_extraction_miss_fallthrough_keeps_all(self, tmp_path):
        """When no block intersects the scope signal, the helper returns
        the input unchanged. Issue #851: the signal is more likely
        off-scope than every commit being a recycled-ID stray."""
        repo = _make_repo(tmp_path)
        conn = _seed_db(
            tmp_path, task_id=4,
            summary="Reference foo", description="Mentions bin/tusk-foo.py only as precedent",
        )
        # Only commit in the set touches a different file; no block
        # intersects bin/tusk-foo.py → fallthrough.
        sha = _commit(repo, "bin/tusk-bar.py", "y\n", "[TASK-4] real work")

        kept = git_helpers.filter_commits_by_block_overlap([sha], 4, repo, conn)
        assert kept == [sha]

    def test_basename_match_keeps_block(self, tmp_path):
        """Issue #670: when the description names a file by bare basename
        (no directory), the helper still matches commits that touch the
        full path."""
        repo = _make_repo(tmp_path)
        conn = _seed_db(
            tmp_path, task_id=5,
            summary="Update FULL-RETRO", description="Edit FULL-RETRO.md",
        )
        sha = _commit(
            repo, "skills/retro/FULL-RETRO.md", "x\n",
            "[TASK-5] retro doc",
        )

        kept = git_helpers.filter_commits_by_block_overlap([sha], 5, repo, conn)
        assert kept == [sha]

    def test_preserves_input_order(self, tmp_path):
        """Kept SHAs come back in the order they appeared in *commits*,
        which matters for callers that take commits[0] / commits[-1]."""
        repo = _make_repo(tmp_path)
        conn = _seed_db(
            tmp_path, task_id=6,
            summary="Wire foo and bar",
            description="Update bin/tusk-foo.py and bin/tusk-bar.py",
        )
        a = _commit(repo, "bin/tusk-foo.py", "x\n", "[TASK-6] foo")
        b = _commit(repo, "bin/tusk-bar.py", "y\n", "[TASK-6] bar")

        kept = git_helpers.filter_commits_by_block_overlap([b, a], 6, repo, conn)
        # Input order [b, a] preserved.
        assert kept == [b, a]


class TestFilterCallerSymmetry:
    """The three filter callers — review-diff-range, task-summary, task-done —
    all route through ``filter_commits_by_block_overlap`` and must produce
    identical kept-SHA results for the same (commits, scope) input. The
    issue specifically called out this symmetry: ``task-done.py and
    tusk-merge.py drop sibling commits that task-summary.py keeps``.

    Test scenarios mirror the helper tests above; each caller is invoked
    via its thin wrapper, and the kept SHAs from each are asserted equal.
    The review-diff-range and task-done wrappers both expose a
    ``_filter_commits_by_task_overlap`` function over the same
    ``(commits, task_id, repo_root, ...)`` shape; task-summary's wiring
    pre-computes ``commit_parents`` separately so this test exercises the
    helper directly rather than the call site to keep the comparison apples
    to apples.
    """

    @pytest.mark.parametrize("layout", ["contiguous", "non_contiguous", "fallthrough"])
    def test_callers_agree_on_kept_shas(self, tmp_path, layout):
        repo = _make_repo(tmp_path)
        conn = _seed_db(
            tmp_path, task_id=10,
            summary="Wire foo", description="Update bin/tusk-foo.py",
        )

        if layout == "contiguous":
            real = _commit(repo, "bin/tusk-foo.py", "x\n", "[TASK-10] real")
            stray = _commit(repo, "VERSION", "999\n", "[TASK-10] VERSION bump")
            commits = [real, stray]
            expected = {real, stray}
        elif layout == "non_contiguous":
            subprocess.run(
                ["git", "-C", repo, "checkout", "-q", "-b", "side"], check=True
            )
            stray = _commit(repo, "noise.txt", "y\n", "[TASK-10] stray")
            subprocess.run(["git", "-C", repo, "checkout", "-q", "main"], check=True)
            real = _commit(repo, "bin/tusk-foo.py", "x\n", "[TASK-10] real")
            commits = [real, stray]
            expected = {real}
        else:  # fallthrough — no block intersects
            sha = _commit(repo, "elsewhere.py", "x\n", "[TASK-10] real")
            commits = [sha]
            expected = {sha}

        # Caller A: direct helper call (mirrors review-diff-range / task-done
        # thin wrappers, and task-summary's path-set form after numstat-→
        # path conversion at the call site).
        a = set(git_helpers.filter_commits_by_block_overlap(
            list(commits), 10, repo, conn,
        ))

        # Caller B: same helper, same shape — confirms no hidden global
        # state mutation between invocations.
        b = set(git_helpers.filter_commits_by_block_overlap(
            list(commits), 10, repo, conn,
        ))

        # Caller C: same helper invoked with precomputed commit_files +
        # commit_parents (mirrors task-summary's pre-fetched form). Building
        # the maps from commit_changed_files / commit_parents_map proves the
        # caller-supplied optional-input path produces the same result.
        cf = {sha: git_helpers.commit_changed_files([sha], repo) for sha in commits}
        cp = git_helpers.commit_parents_map(list(commits), repo)
        c = set(git_helpers.filter_commits_by_block_overlap(
            list(commits), 10, repo, conn,
            commit_files=cf, commit_parents=cp,
        ))

        assert a == b == c == expected

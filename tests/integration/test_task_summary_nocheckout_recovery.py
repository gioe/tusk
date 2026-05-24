"""Regression tests for issues #757/#797/#812/#816/#820/#822/#827.

After ``tusk merge`` uses the no-checkout fast-forward push path (default
branch locked in a sibling worktree), seven independent user reports
documented ``tusk task-summary`` printing ``0 commits / 0 files / +0/-0 lines``
even though the [TASK-N] commits had successfully landed on ``origin/<default>``.

The defense-in-depth fix in ``fetch_diff`` performs a best-effort
``git fetch origin <default>`` when the initial ``git log --all --grep`` scan
returns nothing, then retries the same scan. The fetch refreshes
``refs/remotes/origin/<default>`` in case the push left it stale (some git
configs/network conditions don't update the local remote-tracking ref after a
successful no-default-branch push, contrary to the usual behavior).

These tests reproduce the post-merge state with real git operations:
a primary checkout + a bare remote + a sibling worktree, the no-checkout
push, then explicit cleanup that mirrors what ``tusk merge`` does
(``git worktree remove`` + ``git branch -D``). The "stale remote-tracking
ref" condition is simulated by deleting ``refs/remotes/origin/<default>``
locally before invoking ``fetch_diff`` — exactly the state user reports
indicate the summarizing checkout was in.
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


def _run(cmd, cwd, check=True, env=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _init_repo(repo_root):
    _run(["git", "init", "-q", "-b", "main"], cwd=repo_root)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo_root)
    _run(["git", "config", "user.name", "Test"], cwd=repo_root)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=repo_root)


def _make_db_with_task(tmp_path, task_id, started_at="2026-05-22 00:00:00"):
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
        closed_at TEXT
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
def repo_with_pushed_task(tmp_path):
    """Set up a primary checkout + bare remote + sibling worktree that has pushed
    a [TASK-99] commit directly to origin/main (no-checkout fast-forward), then
    cleaned up the local feature branch as ``tusk merge`` would.

    Returns (primary_path, task_commit_sha).
    """
    bare = tmp_path / "remote.git"
    _run(["git", "init", "-q", "--bare", str(bare)], cwd=str(tmp_path))

    primary = tmp_path / "primary"
    _run(["git", "clone", "-q", str(bare), str(primary)], cwd=str(tmp_path))
    _init_repo(str(primary))
    _run(["git", "commit", "--allow-empty", "-q", "-m", "[INIT] initial"], cwd=str(primary))
    _run(["git", "push", "-q", "origin", "main"], cwd=str(primary))

    sibling = tmp_path / "sibling"
    _run(
        ["git", "worktree", "add", "-q", str(sibling), "-b", "feature/TASK-99-test"],
        cwd=str(primary),
    )
    _init_repo(str(sibling))

    # Make the task commit
    (sibling / "newfile.txt").write_text("changed\n")
    _run(["git", "add", "newfile.txt"], cwd=str(sibling))
    _run(["git", "commit", "-q", "-m", "[TASK-99] add newfile.txt"], cwd=str(sibling))
    task_sha = _run(["git", "rev-parse", "HEAD"], cwd=str(sibling)).stdout.strip()

    # No-checkout fast-forward push: push feature branch directly to origin/main
    _run(["git", "push", "-q", "origin", "feature/TASK-99-test:main"], cwd=str(sibling))

    # Cleanup as tusk merge would
    _run(["git", "worktree", "remove", "-f", str(sibling)], cwd=str(primary), check=False)
    _run(["git", "branch", "-D", "feature/TASK-99-test"], cwd=str(primary), check=False)

    return str(primary), task_sha


class TestFetchDiffNoCheckoutRecovery:
    """Verify fetch_diff recovers diff stats after a no-checkout fast-forward push,
    including the pathological case where refs/remotes/origin/<default> is stale.
    """

    def test_finds_commits_after_no_checkout_push(self, repo_with_pushed_task, tmp_path):
        """Baseline: in the standard post-no-checkout-push state,
        refs/remotes/origin/main has the [TASK-99] commit and fetch_diff
        finds it via --all even without the retry path."""
        primary, _ = repo_with_pushed_task
        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        assert diff["commits"] == 1
        assert diff["files_changed"] == 1
        assert diff["lines_added"] == 1
        assert diff["lines_removed"] == 0

    def test_recovers_when_remote_tracking_ref_is_stale(
        self, repo_with_pushed_task, tmp_path
    ):
        """Simulate the real-world failure mode: refs/remotes/origin/main is
        stale (didn't catch up after the push). fetch_diff must perform a
        best-effort fetch and retry — recovering the [TASK-99] commit
        from the refreshed remote-tracking ref."""
        primary, _ = repo_with_pushed_task

        # Force-stale: rewind the local remote-tracking ref to the pre-push tip.
        # The remote (bare repo) still has the [TASK-99] commit on its main.
        pre_push_sha = _run(
            ["git", "rev-list", "--max-parents=0", "refs/remotes/origin/main"],
            cwd=primary,
        ).stdout.strip()
        _run(
            ["git", "update-ref", "refs/remotes/origin/main", pre_push_sha],
            cwd=primary,
        )

        # Confirm the initial scan would return empty.
        initial = _run(
            [
                "git", "log", "--all",
                "--grep=[TASK-99]",
                "--fixed-strings",
                "--format=%H",
            ],
            cwd=primary,
        ).stdout.strip()
        assert initial == "", (
            "Test precondition failed: expected no [TASK-99] commit visible "
            f"before fetch retry, got: {initial}"
        )

        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()

        # After the retry path: fetch refreshed refs/remotes/origin/main and
        # --all now finds the [TASK-99] commit.
        assert diff["commits"] == 1, (
            f"Expected fetch retry to recover the [TASK-99] commit; "
            f"got diff: {diff}"
        )
        assert diff["files_changed"] == 1
        assert diff["lines_added"] == 1
        assert diff["lines_removed"] == 0

    def test_recovery_does_not_require_commit_hash_on_criteria(
        self, repo_with_pushed_task, tmp_path
    ):
        """Coverage for criterion 1848: the fix must not depend on
        acceptance_criteria.commit_hash being populated. The fetch-and-retry
        path operates on git refs alone, independent of criterion-hash recovery.
        """
        primary, _ = repo_with_pushed_task

        # Stale remote-tracking ref again
        pre_push_sha = _run(
            ["git", "rev-list", "--max-parents=0", "refs/remotes/origin/main"],
            cwd=primary,
        ).stdout.strip()
        _run(
            ["git", "update-ref", "refs/remotes/origin/main", pre_push_sha],
            cwd=primary,
        )

        db_path, conn = _make_db_with_task(tmp_path, 99)
        # Insert a criterion WITHOUT commit_hash — the criterion-hash fallback
        # cannot fire because there is no hash to look up.
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (99, "task is done", 1, None),
        )
        conn.commit()
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()

        assert diff["commits"] == 1
        assert diff["files_changed"] == 1


class TestFetchDiffNoCheckoutRebaseRecovery:
    """The --rebase variant of the no-checkout path (issues #797, #812, #822).
    A rebase rewrites the commit SHAs before push; fetch_diff must still find
    the rewritten commits.
    """

    def test_finds_rebased_commits_pushed_to_origin(self, tmp_path):
        """Set up: primary has an external commit on main; sibling rebases its
        [TASK-77] commit onto origin/main and pushes the rebased tip directly
        to origin/main via the no-checkout path. fetch_diff sees the rewritten
        commit via refs/remotes/origin/main."""
        bare = tmp_path / "remote.git"
        _run(["git", "init", "-q", "--bare", str(bare)], cwd=str(tmp_path))

        primary = tmp_path / "primary"
        _run(["git", "clone", "-q", str(bare), str(primary)], cwd=str(tmp_path))
        _init_repo(str(primary))
        _run(
            ["git", "commit", "--allow-empty", "-q", "-m", "[INIT] initial"],
            cwd=str(primary),
        )
        _run(["git", "push", "-q", "origin", "main"], cwd=str(primary))

        sibling = tmp_path / "sibling"
        _run(
            ["git", "worktree", "add", "-q", str(sibling), "-b", "feature/TASK-77-r"],
            cwd=str(primary),
        )
        _init_repo(str(sibling))

        # Sibling's [TASK-77] commit (pre-rebase)
        (sibling / "feature.txt").write_text("v1\n")
        _run(["git", "add", "feature.txt"], cwd=str(sibling))
        _run(["git", "commit", "-q", "-m", "[TASK-77] feature commit"], cwd=str(sibling))

        # External commit on primary's main (advances origin/main)
        (primary / "external.txt").write_text("external\n")
        _run(["git", "add", "external.txt"], cwd=str(primary))
        _run(["git", "commit", "-q", "-m", "external change"], cwd=str(primary))
        _run(["git", "push", "-q", "origin", "main"], cwd=str(primary))

        # Sibling rebases onto the advanced origin/main and pushes
        _run(["git", "fetch", "-q", "origin"], cwd=str(sibling))
        _run(["git", "rebase", "origin/main"], cwd=str(sibling))
        rebased_sha = _run(
            ["git", "rev-parse", "HEAD"], cwd=str(sibling)
        ).stdout.strip()
        _run(
            ["git", "push", "-q", "origin", "feature/TASK-77-r:main"], cwd=str(sibling)
        )

        # Cleanup as tusk merge would
        _run(
            ["git", "worktree", "remove", "-f", str(sibling)],
            cwd=str(primary),
            check=False,
        )
        _run(
            ["git", "branch", "-D", "feature/TASK-77-r"],
            cwd=str(primary),
            check=False,
        )

        db_path, conn = _make_db_with_task(tmp_path, 77)
        try:
            diff = mod.fetch_diff(77, str(primary), conn=conn)
        finally:
            conn.close()

        assert diff["commits"] == 1, (
            f"Expected the rebased [TASK-77] commit on origin/main to be "
            f"discovered (rewritten SHA: {rebased_sha}); got diff: {diff}"
        )
        assert diff["files_changed"] == 1
        assert diff["lines_added"] == 1


class TestFetchDiffRetryIsOptOutForCommonPath:
    """Performance guard: when the initial scan succeeds, the fetch retry
    must NOT fire — the fetch is a network/disk op and we don't want every
    task-summary call paying that cost.
    """

    def test_fetch_not_invoked_when_initial_scan_succeeds(
        self, repo_with_pushed_task, tmp_path, monkeypatch
    ):
        primary, _ = repo_with_pushed_task

        calls = []
        real_fetch = mod._try_fetch_default_branch

        def _spy(repo_root):
            calls.append(repo_root)
            return real_fetch(repo_root)

        monkeypatch.setattr(mod, "_try_fetch_default_branch", _spy)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()

        assert diff["commits"] == 1
        assert calls == [], (
            "Initial --all scan succeeded; _try_fetch_default_branch should NOT "
            f"have been invoked, but was called: {calls}"
        )

    def test_fetch_invoked_when_initial_scan_empty(
        self, repo_with_pushed_task, tmp_path, monkeypatch
    ):
        primary, _ = repo_with_pushed_task

        # Force-stale to make initial scan empty
        pre_push_sha = _run(
            ["git", "rev-list", "--max-parents=0", "refs/remotes/origin/main"],
            cwd=primary,
        ).stdout.strip()
        _run(
            ["git", "update-ref", "refs/remotes/origin/main", pre_push_sha],
            cwd=primary,
        )

        calls = []
        real_fetch = mod._try_fetch_default_branch

        def _spy(repo_root):
            calls.append(repo_root)
            return real_fetch(repo_root)

        monkeypatch.setattr(mod, "_try_fetch_default_branch", _spy)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()

        assert calls == [primary], (
            f"Expected _try_fetch_default_branch to be invoked exactly once with "
            f"{primary}; got: {calls}"
        )


class TestFetchDiffUnreachableObjectRecovery:
    """Regression coverage for issue #845: the manual ``tusk task-done --reason
    completed`` closeout path when (a) the local remote-tracking ref is missing
    or stale, (b) the TASK-408 fetch retry can't recover the ref (remote URL is
    broken / unreachable), and (c) the criteria carry no commit_hash so the
    criterion-hash fallback also turns up empty.

    The commit object still lives in the shared ``.git/objects`` directory
    because the no-checkout push deposited it before the sibling worktree was
    removed — ``git fsck --unreachable --no-reflogs`` is the only local-only
    mechanism that finds it.
    """

    def _break_remote(self, primary, tmp_path):
        """Force the TASK-408 fetch retry to fail silently: drop the
        remote-tracking ref AND point origin at an unreachable URL so
        ``git fetch origin <default>`` exits non-zero without recovering.
        """
        _run(
            ["git", "update-ref", "-d", "refs/remotes/origin/main"],
            cwd=primary,
        )
        broken = str(tmp_path / "no-such-remote.git")
        _run(["git", "remote", "set-url", "origin", broken], cwd=primary)

    def test_recovers_when_ref_missing_and_remote_unreachable_and_no_commit_hash(
        self, repo_with_pushed_task, tmp_path
    ):
        primary, _ = repo_with_pushed_task
        self._break_remote(primary, tmp_path)

        # Sanity: every ref-based scan must come up empty before the fallback
        # is the only thing that can save us.
        initial = _run(
            ["git", "log", "--all", "--grep=[TASK-99]", "--fixed-strings", "--format=%H"],
            cwd=primary,
        ).stdout.strip()
        assert initial == "", (
            "Test precondition failed: expected --all scan to be empty after "
            f"breaking remote and dropping origin/main; got: {initial}"
        )

        db_path, conn = _make_db_with_task(tmp_path, 99)
        # Criterion with NO commit_hash — the criterion-hash fallback is useless.
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (99, "task is done", 1, None),
        )
        conn.commit()
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()

        assert diff["commits"] == 1, (
            f"Expected fsck unreachable-object recovery to find the [TASK-99] "
            f"commit in the local object store; got diff: {diff}"
        )
        assert diff["files_changed"] == 1
        assert diff["lines_added"] == 1
        assert diff["lines_removed"] == 0

    def test_recovers_when_criterion_hash_is_stale_pre_rebase_sha(
        self, repo_with_pushed_task, tmp_path
    ):
        """The commit_hash recorded on the criterion may point to a pre-rebase
        SHA that's been GC'd. The fallback should still find the post-rebase
        commit via fsck because it lives in the object store (deposited by the
        no-checkout push).
        """
        primary, _ = repo_with_pushed_task
        self._break_remote(primary, tmp_path)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        # Stale commit_hash — points to a SHA that doesn't exist. _criterion_hash_numstats
        # will run `git show <stale>` which fails non-zero and is skipped.
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (99, "task is done", 1, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"),
        )
        conn.commit()
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()

        assert diff["commits"] == 1, (
            f"Expected fsck fallback to recover the real [TASK-99] commit despite "
            f"the stale criterion commit_hash; got diff: {diff}"
        )
        assert diff["files_changed"] == 1

    def test_fsck_not_invoked_when_initial_scan_succeeds(
        self, repo_with_pushed_task, tmp_path, monkeypatch
    ):
        """Performance guard: when ``--all`` finds the commit on the first
        try, the unreachable-object scan must NOT fire. fsck is O(objects)
        and would otherwise penalize every well-merged task.
        """
        primary, _ = repo_with_pushed_task

        calls = []
        real = mod._unreachable_task_commits

        def _spy(task_id, repo_root):
            calls.append((task_id, repo_root))
            return real(task_id, repo_root)

        monkeypatch.setattr(mod, "_unreachable_task_commits", _spy)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()

        assert diff["commits"] == 1
        assert calls == [], (
            "Initial --all scan succeeded; _unreachable_task_commits should "
            f"NOT have been invoked, but was called: {calls}"
        )

    def test_fsck_not_invoked_when_criterion_hash_fallback_succeeds(
        self, repo_with_pushed_task, tmp_path, monkeypatch
    ):
        """When the criterion-hash fallback recovers the commit, fsck must
        not fire — we only pay the fsck cost when every cheaper path is exhausted.
        """
        primary, task_sha = repo_with_pushed_task
        self._break_remote(primary, tmp_path)

        calls = []
        real = mod._unreachable_task_commits

        def _spy(task_id, repo_root):
            calls.append((task_id, repo_root))
            return real(task_id, repo_root)

        monkeypatch.setattr(mod, "_unreachable_task_commits", _spy)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        # Real commit_hash — criterion-hash fallback handles it via `git show`.
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (99, "task is done", 1, task_sha),
        )
        conn.commit()
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()

        assert diff["commits"] == 1
        assert calls == [], (
            "Criterion-hash fallback recovered the commit; "
            f"_unreachable_task_commits should NOT have been invoked: {calls}"
        )


class TestFetchDiffRecoveryTierDiagnostic:
    """Issue #850: when a recovery tier produces commits after a cheaper layer
    came up empty, fetch_diff emits a single stderr line naming the tier.
    The line is TTY-gated identically to bin/tusk's active-projects drift
    warning — silent on captured stderr (agent/CI), forced on with
    ``TUSK_FORCE_WARN=1``, silenced unconditionally by ``TUSK_QUIET=1``.

    Pytest's ``capsys`` captures stderr, which means ``sys.stderr.isatty()``
    returns False during the test — perfect for exercising both gate sides.
    """

    def _break_remote(self, primary, tmp_path):
        _run(
            ["git", "update-ref", "-d", "refs/remotes/origin/main"],
            cwd=primary,
        )
        broken = str(tmp_path / "no-such-remote.git")
        _run(["git", "remote", "set-url", "origin", broken], cwd=primary)

    def _force_stale(self, primary):
        pre_push_sha = _run(
            ["git", "rev-list", "--max-parents=0", "refs/remotes/origin/main"],
            cwd=primary,
        ).stdout.strip()
        _run(
            ["git", "update-ref", "refs/remotes/origin/main", pre_push_sha],
            cwd=primary,
        )

    def test_happy_path_emits_no_diagnostic(
        self, repo_with_pushed_task, tmp_path, capsys, monkeypatch
    ):
        """When the initial --all scan succeeds, no recovery tier fired and
        no diagnostic line may appear — even under TUSK_FORCE_WARN=1."""
        monkeypatch.setenv("TUSK_FORCE_WARN", "1")
        monkeypatch.delenv("TUSK_QUIET", raising=False)
        primary, _ = repo_with_pushed_task
        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        err = capsys.readouterr().err
        assert "recovered diff via" not in err, (
            f"Happy path should emit no recovery-tier diagnostic; got: {err!r}"
        )

    def test_refresh_fetch_tier_emits_diagnostic(
        self, repo_with_pushed_task, tmp_path, capsys, monkeypatch
    ):
        """Tier 1 (TASK-408 refresh-fetch) fires when the initial scan is
        empty but a refreshed remote-tracking ref recovers the commit."""
        monkeypatch.setenv("TUSK_FORCE_WARN", "1")
        monkeypatch.delenv("TUSK_QUIET", raising=False)
        primary, _ = repo_with_pushed_task
        self._force_stale(primary)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        err = capsys.readouterr().err
        assert "recovered diff via refresh-fetch" in err, (
            f"Expected refresh-fetch tier diagnostic; got: {err!r}"
        )

    def test_criterion_hash_tier_emits_diagnostic(
        self, repo_with_pushed_task, tmp_path, capsys, monkeypatch
    ):
        """Tier 2 fires when the refresh-fetch retry also comes up empty
        (broken remote) but the criterion-hash fallback recovers the commit
        via its recorded SHA."""
        monkeypatch.setenv("TUSK_FORCE_WARN", "1")
        monkeypatch.delenv("TUSK_QUIET", raising=False)
        primary, task_sha = repo_with_pushed_task
        self._break_remote(primary, tmp_path)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (99, "task is done", 1, task_sha),
        )
        conn.commit()
        try:
            mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        err = capsys.readouterr().err
        assert "recovered diff via criterion-hash" in err, (
            f"Expected criterion-hash tier diagnostic; got: {err!r}"
        )

    def test_fsck_unreachable_tier_emits_diagnostic(
        self, repo_with_pushed_task, tmp_path, capsys, monkeypatch
    ):
        """Tier 3 (TASK-429 fsck) fires when refresh-fetch and criterion-hash
        both come up empty but the commit is still in the local object store."""
        monkeypatch.setenv("TUSK_FORCE_WARN", "1")
        monkeypatch.delenv("TUSK_QUIET", raising=False)
        primary, _ = repo_with_pushed_task
        self._break_remote(primary, tmp_path)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        # No commit_hash → criterion-hash fallback turns up nothing.
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (99, "task is done", 1, None),
        )
        conn.commit()
        try:
            mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        err = capsys.readouterr().err
        assert "recovered diff via fsck-unreachable" in err, (
            f"Expected fsck-unreachable tier diagnostic; got: {err!r}"
        )

    def test_non_tty_stderr_suppresses_diagnostic(
        self, repo_with_pushed_task, tmp_path, capsys, monkeypatch
    ):
        """Without TUSK_FORCE_WARN, captured (non-TTY) stderr must be silent
        even when a recovery tier fired — agent/CI transcripts stay clean."""
        monkeypatch.delenv("TUSK_FORCE_WARN", raising=False)
        monkeypatch.delenv("TUSK_QUIET", raising=False)
        primary, _ = repo_with_pushed_task
        self._force_stale(primary)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        err = capsys.readouterr().err
        assert "recovered diff via" not in err, (
            f"Captured stderr is non-TTY; diagnostic should be suppressed "
            f"without TUSK_FORCE_WARN; got: {err!r}"
        )

    def test_tusk_quiet_overrides_force_warn(
        self, repo_with_pushed_task, tmp_path, capsys, monkeypatch
    ):
        """TUSK_QUIET=1 silences the diagnostic even when TUSK_FORCE_WARN=1
        is also set — mirrors maybe_warn_cross_repo_drift's ordering."""
        monkeypatch.setenv("TUSK_QUIET", "1")
        monkeypatch.setenv("TUSK_FORCE_WARN", "1")
        primary, _ = repo_with_pushed_task
        self._force_stale(primary)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        err = capsys.readouterr().err
        assert "recovered diff via" not in err, (
            f"TUSK_QUIET=1 must override TUSK_FORCE_WARN=1; got: {err!r}"
        )


class TestFetchDiffRecoveredViaField:
    """Issue #852: the recovery-tier diagnostic is TTY-gated, but agent callers
    capture stderr and never see it. The diff dict returned by ``fetch_diff``
    must include a ``recovered_via`` field naming the tier that produced the
    final result — ``"refresh-fetch"`` / ``"criterion-hash"`` / ``"fsck-unreachable"``
    / ``None`` — so JSON consumers (``tusk task-summary --format json``)
    can answer "why are my stats zero" from the output alone.
    """

    def _break_remote(self, primary, tmp_path):
        _run(
            ["git", "update-ref", "-d", "refs/remotes/origin/main"],
            cwd=primary,
        )
        broken = str(tmp_path / "no-such-remote.git")
        _run(["git", "remote", "set-url", "origin", broken], cwd=primary)

    def _force_stale(self, primary):
        pre_push_sha = _run(
            ["git", "rev-list", "--max-parents=0", "refs/remotes/origin/main"],
            cwd=primary,
        ).stdout.strip()
        _run(
            ["git", "update-ref", "refs/remotes/origin/main", pre_push_sha],
            cwd=primary,
        )

    def test_cheap_path_emits_null_recovered_via(self, repo_with_pushed_task, tmp_path):
        """Initial --all scan succeeds → no tier fired → recovered_via is None.
        Crucially, the key must be present (not missing) so JSON consumers
        can read ``.diff.recovered_via`` unconditionally."""
        primary, _ = repo_with_pushed_task
        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        assert "recovered_via" in diff, (
            f"recovered_via must be present even on the cheap path; "
            f"got keys: {sorted(diff.keys())}"
        )
        assert diff["recovered_via"] is None

    def test_git_log_failure_zero_path_emits_null_recovered_via(self, tmp_path):
        """Early-return zero path (git log fails before any tier could fire)
        must also include ``recovered_via: None`` so callers can read the field
        unconditionally on the failure path."""
        # Non-existent repo root → subprocess raises and fetch_diff returns zero.
        bogus = str(tmp_path / "does-not-exist")
        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            diff = mod.fetch_diff(99, bogus, conn=conn)
        finally:
            conn.close()
        assert diff == {
            "commits": 0,
            "files_changed": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "recovered_via": None,
        }

    def test_refresh_fetch_tier_surfaces_in_recovered_via(
        self, repo_with_pushed_task, tmp_path
    ):
        primary, _ = repo_with_pushed_task
        self._force_stale(primary)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        assert diff["recovered_via"] == "refresh-fetch", (
            f"Tier 1 should set recovered_via='refresh-fetch'; got: {diff!r}"
        )
        # Sanity: recovery actually produced the commit
        assert diff["commits"] == 1

    def test_criterion_hash_tier_surfaces_in_recovered_via(
        self, repo_with_pushed_task, tmp_path
    ):
        primary, task_sha = repo_with_pushed_task
        self._break_remote(primary, tmp_path)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (99, "task is done", 1, task_sha),
        )
        conn.commit()
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        assert diff["recovered_via"] == "criterion-hash", (
            f"Tier 2 should set recovered_via='criterion-hash'; got: {diff!r}"
        )
        assert diff["commits"] == 1

    def test_fsck_unreachable_tier_surfaces_in_recovered_via(
        self, repo_with_pushed_task, tmp_path
    ):
        primary, _ = repo_with_pushed_task
        self._break_remote(primary, tmp_path)

        db_path, conn = _make_db_with_task(tmp_path, 99)
        # No commit_hash → criterion-hash fallback turns up nothing.
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_completed, commit_hash) VALUES (?, ?, ?, ?)",
            (99, "task is done", 1, None),
        )
        conn.commit()
        try:
            diff = mod.fetch_diff(99, primary, conn=conn)
        finally:
            conn.close()
        assert diff["recovered_via"] == "fsck-unreachable", (
            f"Tier 3 should set recovered_via='fsck-unreachable'; got: {diff!r}"
        )
        assert diff["commits"] == 1

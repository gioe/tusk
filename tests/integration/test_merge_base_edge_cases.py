"""Regression tests for ``_resolve_merge_base`` and its no-checkout call site.

Issue #879, follow-up to TASK-452 (migration 72 range-aware fetch_diff fast-path).
``bin/tusk-merge.py::_resolve_merge_base`` used to return the first non-empty
``git merge-base`` across ``(origin/<default_branch>, <default_branch>)`` —
two refs that disagree whenever local default is ahead of origin (the operator
has unpushed commits there). Two distinct edges silently produced wrong
``fetch_diff`` stats:

* **Edge 1 (local default ahead of origin):** ``git merge-base origin/<default>
  <feature>`` returns an older common ancestor than ``git merge-base <default>
  <feature>``. Stamping the older base means
  ``git log --first-parent --numstat <old_base>..<tip>`` over-includes the
  local-default unpushed commits — inflated files/lines on the task's diff.
* **Edge 2 (origin already contains feature tip):** ``_origin_already_contains``
  is true (work shipped in a prior session). ``git merge-base origin/<default>
  <feature>`` returns the feature tip; the ``fetch_diff`` dispatcher sees
  ``base == tip`` and collapses into single-SHA ``git show <tip>`` mode —
  tip-only stats instead of the cumulative range.

The fix lives in two co-located pieces. ``_resolve_merge_base`` now resolves
both candidate refs and uses ``git merge-base --is-ancestor`` to pick the
descendant of the two. The no-checkout call site in ``_no_checkout_push_path``
sets both merge stamp values to None inside the
``_origin_already_contains`` branch so the task row stays unstamped and ``fetch_diff``
falls through to the recovery chain (which reproduces cumulative stats from
the actual ``[TASK-N]`` commit history).
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

from tests.integration.conftest import _insert_session, _insert_task

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(filename: str):
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_").rsplit(".", 1)[0],
        os.path.join(BIN, filename),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_merge = _load("tusk-merge.py")
tusk_task_summary = _load("tusk-task-summary.py")


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


def _make_db_with_task(tmp_path, task_id, started_at="2026-05-25 00:00:00"):
    """Minimal schema slice satisfying ``fetch_diff``'s reads.

    Mirrors the fixture in ``test_task_summary_multi_commit_ff.py`` — both
    columns the v72 fast-path SELECTs (``merge_commit_sha`` and
    ``merge_base_sha``), plus ``commit_hash`` on criteria so the recovery
    chain's hash tier remains testable when stamps are cleared.
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


def _build_ahead_of_origin_repo(tmp_path, task_id):
    """Build a repo where local main is ahead of origin/main, and the
    feature branch was cut from the ahead-of-origin local tip.

    Topology after setup::

        origin/main:  A
        local main:   A -> B -> C   (B, C are local-only — unpushed)
        feature:      A -> B -> C -> F1 -> F2   ([TASK-N] commits)

    The two merge-base candidates disagree:

    * ``git merge-base origin/main feature`` → ``A`` (older)
    * ``git merge-base main       feature`` → ``C`` (descendant)

    ``_resolve_merge_base`` must return ``C`` so the range
    ``git log --first-parent --numstat C..tip`` excludes ``B`` and ``C``
    from the task's stats. Returning ``A`` (the old behavior) would
    over-include the unpushed local-default commits.

    Returns ``(repo_path, base_origin_sha, base_local_sha, feature_tip_sha)``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    origin = tmp_path / "origin.git"
    _init_repo(str(repo))
    _run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], cwd=str(tmp_path))
    _run(["git", "remote", "add", "origin", str(origin)], cwd=str(repo))

    # Commit A — initial state shared by origin and local.
    (repo / "README.md").write_text("init\n")
    _run(["git", "add", "README.md"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[INIT] initial"], cwd=str(repo))
    base_origin_sha = _run(["git", "rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()
    _run(["git", "push", "-q", "origin", "main"], cwd=str(repo))

    # Commits B, C — local-only on main, NOT pushed. Touch unrelated files
    # so we can later assert their contents are NOT in the task's stats.
    (repo / "unrelated_b.txt").write_text("local b\n")
    _run(["git", "add", "unrelated_b.txt"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[LOCAL] unpushed b"], cwd=str(repo))

    (repo / "unrelated_c.txt").write_text("local c\nline2\n")
    _run(["git", "add", "unrelated_c.txt"], cwd=str(repo))
    _run(["git", "commit", "-q", "-m", "[LOCAL] unpushed c"], cwd=str(repo))
    base_local_sha = _run(["git", "rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()

    # Branch feature from C (the ahead-of-origin local tip), add task commits.
    branch = f"feature/TASK-{task_id}-edge1"
    _run(["git", "checkout", "-q", "-b", branch], cwd=str(repo))

    (repo / "task_a.txt").write_text("task line\n")
    _run(["git", "add", "task_a.txt"], cwd=str(repo))
    _run(
        ["git", "commit", "-q", "-m", f"[TASK-{task_id}] add task_a"],
        cwd=str(repo),
    )

    (repo / "task_b.txt").write_text("two\nlines\n")
    _run(["git", "add", "task_b.txt"], cwd=str(repo))
    _run(
        ["git", "commit", "-q", "-m", f"[TASK-{task_id}] add task_b"],
        cwd=str(repo),
    )
    feature_tip = _run(["git", "rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()

    return str(repo), base_origin_sha, base_local_sha, feature_tip, branch


class TestResolveMergeBaseDescendantSelection:
    """Criterion 2117: when ``origin/<default>`` and the local
    ``<default>`` resolve to different merge-bases, the helper returns
    the descendant of the two."""

    def test_returns_descendant_when_local_default_ahead_of_origin(
        self, tmp_path, monkeypatch
    ):
        repo, base_origin, base_local, _tip, branch = _build_ahead_of_origin_repo(
            tmp_path, task_id=900
        )
        # The two candidate refs must actually disagree — otherwise this test
        # is a no-op and any helper implementation passes.
        assert base_origin != base_local, (
            "fixture sanity: expected origin/main and local main to diverge"
        )

        # _resolve_merge_base shells out to git from the current cwd, so
        # operate inside the prepared repo.
        monkeypatch.chdir(repo)
        result = tusk_merge._resolve_merge_base(branch, "main")
        assert result == base_local, (
            f"expected descendant base {base_local!r} (local main tip), "
            f"got {result!r}. The old code path returned the ancestor "
            f"{base_origin!r} because it accepted origin/main's merge-base "
            f"first without checking which candidate was the descendant."
        )

    def test_returns_single_candidate_when_refs_agree(self, tmp_path, monkeypatch):
        """When local main matches origin/main both candidates collapse
        to the same SHA — helper returns it unchanged (no descendant
        comparison needed)."""
        repo = tmp_path / "agree"
        repo.mkdir()
        _init_repo(str(repo))

        (repo / "README.md").write_text("init\n")
        _run(["git", "add", "README.md"], cwd=str(repo))
        _run(["git", "commit", "-q", "-m", "[INIT] initial"], cwd=str(repo))
        # No origin remote at all — both candidates resolve to the same SHA
        # (local main); the function still returns that SHA rather than
        # None. The origin/main probe simply fails silently.
        head = _run(["git", "rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()

        _run(["git", "checkout", "-q", "-b", "feature/TASK-901-agree"], cwd=str(repo))
        (repo / "f.txt").write_text("f\n")
        _run(["git", "add", "f.txt"], cwd=str(repo))
        _run(["git", "commit", "-q", "-m", "[TASK-901] add f"], cwd=str(repo))

        monkeypatch.chdir(str(repo))
        result = tusk_merge._resolve_merge_base("feature/TASK-901-agree", "main")
        assert result == head, (
            f"expected the single resolvable base {head!r}, got {result!r}"
        )

    def test_returns_none_when_both_refs_unresolvable(self, tmp_path, monkeypatch):
        """Neither ``origin/<default>`` nor the local ``<default>``
        resolves — caller falls back to NULL-base stamping (issue #849
        recovery chain)."""
        repo = tmp_path / "empty"
        repo.mkdir()
        _init_repo(str(repo))
        monkeypatch.chdir(str(repo))
        result = tusk_merge._resolve_merge_base("does-not-exist", "main")
        assert result is None


class TestEdge1AheadOfOriginFetchDiffStats:
    """Criterion 2118: with the helper now picking the descendant,
    ``fetch_diff``'s range query reports only the task commits — not
    the local-default unpushed commits B and C from the fixture."""

    def test_fetch_diff_excludes_unpushed_local_default_commits(self, tmp_path):
        task_id = 902
        repo, _base_origin, base_local, tip, _branch = (
            _build_ahead_of_origin_repo(tmp_path, task_id)
        )

        db_path, conn = _make_db_with_task(tmp_path, task_id)
        try:
            # Stamp the descendant base (what the fixed helper now returns).
            conn.execute(
                "UPDATE tasks SET merge_commit_sha = ?, merge_base_sha = ? "
                "WHERE id = ?",
                (tip, base_local, task_id),
            )
            conn.commit()
            descendant_stats = tusk_task_summary.fetch_diff(
                task_id, repo, conn=conn
            )

            # Then stamp the ANCESTOR (what the old broken helper returned)
            # to demonstrate the inflation that the fix prevents.
            conn.execute(
                "UPDATE tasks SET merge_base_sha = ? WHERE id = ?",
                (_base_origin, task_id),
            )
            conn.commit()
            ancestor_stats = tusk_task_summary.fetch_diff(
                task_id, repo, conn=conn
            )
        finally:
            conn.close()

        # Descendant base — task work only: 2 commits, 2 distinct files
        # (task_a.txt, task_b.txt), 1 + 2 = 3 added lines.
        assert descendant_stats["commits"] == 2
        assert descendant_stats["files_changed"] == 2
        assert descendant_stats["lines_added"] == 3
        assert descendant_stats["lines_removed"] == 0
        assert descendant_stats["recovered_via"] == "stamped-sha"

        # Ancestor base — the OLD inflated stats. Includes the two unpushed
        # [LOCAL] commits (unrelated_b.txt and unrelated_c.txt), so 4 commits
        # / 4 files / 1+2+1+2 = 6 added lines. This branch is the regression
        # this test guards against. If the helper ever reverts to "first
        # non-empty wins", fetch_diff would return THESE numbers for real
        # tasks — silently inflated.
        assert ancestor_stats["commits"] == 4
        assert ancestor_stats["files_changed"] == 4
        assert ancestor_stats["lines_added"] == 6

    def test_descendant_stats_match_recovery_chain(self, tmp_path):
        """Cross-check: the descendant-base fast-path matches what the
        cheap ``git log --all --grep`` recovery scan would produce from
        the same git history. Mirrors the parity check from TASK-452's
        ``test_task_summary_multi_commit_ff.py::test_range_mode_matches_recovery_chain_output``.
        """
        task_id = 903
        repo, _base_origin, base_local, tip, _branch = (
            _build_ahead_of_origin_repo(tmp_path, task_id)
        )

        db_path, conn = _make_db_with_task(tmp_path, task_id)
        try:
            conn.execute(
                "UPDATE tasks SET merge_commit_sha = ?, merge_base_sha = ? "
                "WHERE id = ?",
                (tip, base_local, task_id),
            )
            conn.commit()
            fast = tusk_task_summary.fetch_diff(task_id, repo, conn=conn)

            conn.execute(
                "UPDATE tasks SET merge_commit_sha = NULL, merge_base_sha = NULL "
                "WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            scan = tusk_task_summary.fetch_diff(task_id, repo, conn=conn)
        finally:
            conn.close()

        for key in ("commits", "files_changed", "lines_added", "lines_removed"):
            assert fast[key] == scan[key], (
                f"descendant-base fast-path disagrees with recovery scan on "
                f"{key}: fast={fast[key]}, scan={scan[key]}"
            )
        assert fast["recovered_via"] == "stamped-sha"
        assert scan["recovered_via"] is None


class TestEdge2NoCheckoutStampsNullWhenOriginAlreadyContains:
    """Criterion 2119: when ``_origin_already_contains(branch, default)``
    is true, the no-checkout push path skips ``_resolve_merge_base``
    entirely and the ``_close_completed_task`` call receives
    ``merge_base_sha=None``. ``fetch_diff`` then falls through to the
    recovery chain (cumulative stats) rather than collapsing into
    single-SHA tip-only mode (the regression this test guards against).
    """

    def _mock_run_factory(
        self,
        *,
        branch_name: str,
        task_id: int,
        default_branch: str = "main",
        record_calls: list | None = None,
    ):
        calls = record_calls if record_calls is not None else []
        # _origin_already_contains uses ``git rev-list origin/<default>..<branch>
        # --count`` and treats stdout == "0" as "origin already has it".
        # Default _resolve_merge_base behavior: return a synthetic SHA so the
        # old (broken) code path WOULD have stamped it — making the
        # negative assertion (stamp == None) meaningful.
        SYNTHETIC_BASE = "deadbeef" * 5

        def _run(args, check=True):
            calls.append(list(args))

            if args[:2] == ["git", "diff"] and "--name-only" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "stash", "push"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="No local changes to save", stderr=""
                )
            if args[:3] == ["git", "remote", "get-url"]:
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout="git@example.com:owner/repo.git\n",
                    stderr="",
                )
            if args[:4] == ["git", "worktree", "list", "--porcelain"]:
                # Pretend default branch is locked in another worktree so the
                # no-checkout path is the chosen one.
                stdout = (
                    f"worktree /tmp/repo-main\n"
                    f"HEAD abc123\n"
                    f"branch refs/heads/{default_branch}\n"
                )
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
            if args[:2] == ["git", "checkout"] and args[2:3] == [default_branch]:
                return subprocess.CompletedProcess(
                    args, 128, stdout="",
                    stderr=(
                        f"fatal: '{default_branch}' is already used by worktree "
                        "at '/tmp/repo-main'\n"
                    ),
                )
            if (
                args[:3] == ["git", "rev-parse", "--verify"]
                and args[3:4] == [f"refs/remotes/origin/{default_branch}"]
            ):
                return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
            if (
                args[:2] == ["git", "log"]
                and any(
                    a == f"refs/remotes/origin/{default_branch}..{default_branch}"
                    for a in args
                )
            ):
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "fetch", "origin"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # ``_origin_already_contains`` — count zero means origin already
            # contains every commit the push would land. This is the trigger
            # for the bug under test.
            if (
                args[:2] == ["git", "rev-list"]
                and any(
                    a == f"origin/{default_branch}..{branch_name}"
                    for a in args
                )
                and "--count" in args
            ):
                return subprocess.CompletedProcess(args, 0, stdout="0\n", stderr="")
            # The is-ancestor check used by _branch_contains_origin and the
            # descendant selection in _resolve_merge_base.
            if args[:3] == ["git", "merge-base", "--is-ancestor"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # The actual merge-base resolution — if (incorrectly) called
            # despite origin_already_contains=True, would return this
            # synthetic SHA. The whole point of the fix is that it should
            # NOT be reached in this scenario.
            if args[:2] == ["git", "merge-base"] and "--is-ancestor" not in args:
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"{SYNTHETIC_BASE}\n", stderr=""
                )
            if args[:2] == ["git", "rev-parse"] and args[2:3] == [branch_name]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="cafef00d" * 5 + "\n", stderr=""
                )
            if args[:3] == ["git", "config", "--get"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            if args[:3] == ["git", "push", "origin"] and args[3:5] == [
                "--delete", branch_name,
            ]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if "session-close" in args:
                return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
            if "task-done" in args:
                payload = json.dumps({
                    "task": {
                        "id": task_id,
                        "status": "Done",
                        "closed_reason": "completed",
                    },
                    "sessions_closed": 0,
                    "unblocked_tasks": [],
                })
                return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        return _run, calls

    def test_no_checkout_skips_helper_and_leaves_task_unstamped(
        self, db_path, config_path, monkeypatch
    ):
        task_id, session_id = self._setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-edge2"
        record = []

        # Capture the (task_id, merge_commit_sha, merge_base_sha) the
        # no-checkout path threads into the close-out helper.
        stamp_calls: list[dict] = []

        def _capture_stamp(db, tid, commit_sha=None, base_sha=None):
            stamp_calls.append({
                "task_id": tid,
                "merge_commit_sha": commit_sha,
                "merge_base_sha": base_sha,
            })

        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (branch, None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        monkeypatch.setattr(
            tusk_merge, "_stamp_merge_commit_sha", _capture_stamp
        )

        mock_run, _ = self._mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id),
                 "--session", str(session_id)]
            )

        assert rc == 0, (
            f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"
        )
        # Sanity: the no-checkout path was selected (no ff-only merge) and
        # the push was skipped because origin already contains the tip.
        stderr = stderr_buf.getvalue()
        assert "already contains" in stderr
        assert "continuing task finalization" in stderr
        assert f"tusk merge {task_id} --session {session_id}" in stderr
        assert not [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert ["git", "push", "origin", f"{branch}:main"] not in record

        # The actual assertion: when origin already contains the feature
        # tip, ``_resolve_merge_base`` is NOT called (so the synthetic SHA
        # the mock would have returned never reaches the stamper) and the
        # stamp values passed for the task are both None. A tip with no base
        # routes task-summary through tip-only mode and recreates issue #1239.
        assert stamp_calls, (
            "expected _stamp_merge_commit_sha to be invoked during close-out"
        )
        # The relevant call is the one for THIS task (the close-out path
        # may invoke the stamper once per session-finalize).
        task_stamps = [c for c in stamp_calls if c["task_id"] == task_id]
        assert task_stamps, (
            f"expected stamp call for task {task_id}; got: {stamp_calls}"
        )
        assert task_stamps[-1]["merge_commit_sha"] is None, (
            f"expected merge_commit_sha to be None in the "
            f"origin-already-contains branch; got {task_stamps[-1]!r}"
        )
        assert task_stamps[-1]["merge_base_sha"] is None, (
            f"expected merge_base_sha to be None in the origin-already-contains "
            f"branch (issue #879 Edge 2); got {task_stamps[-1]!r}. A non-None "
            f"value means _resolve_merge_base was called despite the early "
            f"skip — the fix at the call site has regressed."
        )

    @staticmethod
    def _setup_task_session(db_path):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        return task_id, session_id

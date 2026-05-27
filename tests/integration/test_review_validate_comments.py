"""Integration tests for ``tusk review validate-comments`` (issue #783).

The reviewer agent has been observed fabricating findings that reference
files outside the actual diff — the orchestrator-side validator built in
TASK-393 enforces an objective ground truth: any pending comment whose
``file_path`` is not in ``git diff --name-only <range>`` is auto-resolved
as ``dismissed`` with an explanatory ``resolution_note``.

These tests exercise the real CLI against a real DB + git repo, mirroring
the ``test_review_begin_worktree.py`` integration-test pattern.
"""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(args, *, cwd, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _git(args, *, cwd, env=None):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return result


def _repo_with_tusk(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)

    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    monkeypatch.setenv("TUSK_DB", str(db_path))
    monkeypatch.setenv("TUSK_QUIET", "1")

    result = _run(["init", "--force", "--skip-gitignore"], cwd=repo, env=env)
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return repo, db_path, env


def _insert_in_progress_task(db_path, summary="validate-comments task"):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score, started_at) "
            "VALUES (?, ?, 'In Progress', 'feature', 'High', 'M', 30, datetime('now'))",
            (summary, "exercise reviewer-comment fabrication guard"),
        )
        conn.commit()
        return cur.lastrowid


class TestValidateComments:
    def test_dismisses_fabricated_file_path(self, tmp_path, monkeypatch):
        """A pending comment whose file_path is not in the diff must be
        auto-dismissed with a resolution_note that names both the offending
        path and the diff range. The comment's resolution becomes
        ``dismissed`` and persists across CLI invocations."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)

        # Create a feature branch with a real commit touching real.py only.
        _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], cwd=repo)
        (repo / "real.py").write_text("def real():\n    return 1\n", encoding="utf-8")
        _git(["add", "real.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] real"], cwd=repo, env=env)

        # tusk review begin → creates a code_reviews row, returns review_id.
        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        # Add three comments: one for a real file, one for a fabricated
        # file, and one general (file_path NULL).
        for args in [
            ["review", "add-comment", str(review_id), "real issue", "--file", "real.py", "--line-start", "1", "--category", "must_fix", "--severity", "minor"],
            ["review", "add-comment", str(review_id), "fabricated", "--file", "src/never_existed.py", "--line-start", "42", "--category", "must_fix", "--severity", "minor"],
            ["review", "add-comment", str(review_id), "general remark", "--category", "suggest", "--severity", "minor"],
        ]:
            r = _run(args, cwd=repo, env=env)
            assert r.returncode == 0, r.stderr

        # Validate comments.
        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["review_id"] == review_id
        assert payload["validated"] == 3
        assert payload["in_diff"] == 1
        assert payload["general"] == 1
        assert len(payload["dismissed"]) == 1
        assert payload["dismissed"][0]["file_path"] == "src/never_existed.py"
        assert payload["dismissed_general"] == []
        assert "real.py" in payload["diff_files"]

        # Confirm the dismissal landed in the DB with a non-empty note.
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT file_path, resolution, resolution_note FROM review_comments"
                " WHERE review_id = ? ORDER BY id",
                (review_id,),
            ).fetchall()

        kept_real = next(r for r in rows if r["file_path"] == "real.py")
        dismissed = next(r for r in rows if r["file_path"] == "src/never_existed.py")
        general = next(r for r in rows if r["file_path"] is None)

        assert kept_real["resolution"] is None, "real-file comment must be untouched"
        assert dismissed["resolution"] == "dismissed"
        assert "src/never_existed.py" in (dismissed["resolution_note"] or "")
        assert "issue #783" in (dismissed["resolution_note"] or "")
        assert general["resolution"] is None, "general (file_path=null) must be untouched"

    def test_no_dismissals_when_every_path_in_diff(self, tmp_path, monkeypatch):
        """All file_paths in the diff → zero dismissals; comments stay open."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)

        _git(["checkout", "-b", f"feature/TASK-{task_id}-clean"], cwd=repo)
        (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
        _git(["add", "a.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] add a"], cwd=repo, env=env)

        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        r = _run(
            ["review", "add-comment", str(review_id), "real", "--file", "a.py", "--line-start", "1", "--category", "suggest", "--severity", "minor"],
            cwd=repo,
            env=env,
        )
        assert r.returncode == 0, r.stderr

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["dismissed"] == []
        assert payload["in_diff"] == 1
        assert payload["validated"] == 1

    def test_unknown_review_id_exits_two(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        result = _run(["review", "validate-comments", "99999"], cwd=repo, env=env)
        assert result.returncode == 2
        assert "Review 99999 not found" in result.stderr

    def test_primary_cwd_with_unpushed_main_does_not_dismiss_real_findings(
        self, tmp_path, monkeypatch
    ):
        """Issue #821 / TASK-412: when invoked from the primary repo's CWD,
        validate-comments must compute its diff against the *feature-branch*
        commits — not against unpushed local-default commits that happen to
        share the primary checkout. Before the fix, ``origin/main...HEAD`` in
        the primary returned the unpushed-local-main diff, and every legitimate
        review comment whose ``file_path`` was on the feature branch was
        silently dismissed under the issue #783 fabrication-guard rationale.
        """
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)

        # Stand up an origin/main pointing at the seed commit so the primary
        # range is `origin/main...HEAD`.
        seed_sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        _git(["update-ref", "refs/remotes/origin/main", seed_sha], cwd=repo)

        # Unpushed local commit on main — NOT tagged [TASK-N]. This is the
        # shape that caused #821: the orchestrator's CWD diff resolves
        # against this commit, hiding the real feature branch.
        (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        _git(["add", "unrelated.txt"], cwd=repo, env=env)
        _git(["commit", "-m", "Unrelated local change"], cwd=repo, env=env)

        # Sibling worktree carries the feature branch with the real commit.
        sibling = tmp_path / "repo-wt"
        _git(
            ["worktree", "add", "-b", f"feature/TASK-{task_id}-x",
             str(sibling), "refs/remotes/origin/main"],
            cwd=repo,
        )
        (sibling / "task.py").write_text("def task():\n    return 1\n", encoding="utf-8")
        _git(["add", "task.py"], cwd=sibling, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] task work"], cwd=sibling, env=env)

        # Invoke review begin + validate-comments from the PRIMARY repo CWD.
        # This is exactly the orchestrator-side invocation pattern from #821.
        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        # Add one in-diff comment (task.py, feature-branch only) and one
        # genuinely fabricated comment (src/fake.py, never exists).
        for args in [
            ["review", "add-comment", str(review_id), "real issue on task.py",
             "--file", "task.py", "--line-start", "1",
             "--category", "must_fix", "--severity", "minor"],
            ["review", "add-comment", str(review_id), "fabricated path",
             "--file", "src/fake.py", "--line-start", "10",
             "--category", "must_fix", "--severity", "minor"],
        ]:
            r = _run(args, cwd=repo, env=env)
            assert r.returncode == 0, r.stderr

        # Run validate-comments from the primary CWD.
        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        # task.py is the file the feature branch added — it must be in the
        # diff_files list and the in-diff comment must NOT be dismissed.
        assert "task.py" in payload["diff_files"], (
            f"task.py expected in diff_files, got {payload['diff_files']}"
        )
        # unrelated.txt is the orchestrator's unpushed-local-main shape; it
        # must NOT contaminate the diff_files list.
        assert "unrelated.txt" not in payload["diff_files"], (
            f"unrelated.txt must not appear in diff_files, got {payload['diff_files']}"
        )
        # Only the fabricated comment should be dismissed.
        assert len(payload["dismissed"]) == 1
        assert payload["dismissed"][0]["file_path"] == "src/fake.py"
        assert payload["in_diff"] == 1

        # Confirm in the DB: real comment untouched, fabricated dismissed.
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT file_path, resolution FROM review_comments"
                " WHERE review_id = ? ORDER BY id",
                (review_id,),
            ).fetchall()
        kept = next(r for r in rows if r["file_path"] == "task.py")
        dismissed = next(r for r in rows if r["file_path"] == "src/fake.py")
        assert kept["resolution"] is None, "task.py comment must NOT be dismissed"
        assert dismissed["resolution"] == "dismissed"

    def test_review_begin_stamps_diff_range_on_row_847(self, tmp_path, monkeypatch):
        """Issue #847: ``tusk review begin`` must populate
        ``code_reviews.diff_range`` with the resolved range so
        ``tusk review validate-comments`` can reuse it instead of
        re-deriving via ``compute_range``."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)
        _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], cwd=repo)
        (repo / "feat.py").write_text("x\n", encoding="utf-8")
        _git(["add", "feat.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] feat"], cwd=repo, env=env)

        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        payload = json.loads(begin.stdout)
        review_id = payload["review_id"]
        returned_range = payload["range"]

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT diff_range FROM code_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == returned_range, (
            f"stamped diff_range {row[0]!r} should match the returned range "
            f"{returned_range!r}"
        )

    def test_validate_comments_uses_stored_diff_range_not_recompute_847(
        self, tmp_path, monkeypatch
    ):
        """Issue #847 regression: ``validate-comments`` must use the
        ``diff_range`` stamped by ``tusk review begin``, not re-derive
        via ``compute_range``. Demonstrated by manually overwriting the
        stored value with a range that excludes the comment's file —
        if the validator recomputed, the file would still be in the diff
        and the comment would survive; using the (overwritten) stored
        range dismisses it."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)
        _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], cwd=repo)
        (repo / "feat.py").write_text("x\n", encoding="utf-8")
        _git(["add", "feat.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] feat"], cwd=repo, env=env)
        feat_sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        # Add a comment on feat.py. With the begin-time range, this file IS
        # in the diff — under recompute behavior the comment would NOT be
        # dismissed.
        r = _run(
            ["review", "add-comment", str(review_id), "issue on feat",
             "--file", "feat.py", "--line-start", "1",
             "--category", "must_fix", "--severity", "minor"],
            cwd=repo, env=env,
        )
        assert r.returncode == 0, r.stderr

        # Overwrite the stored diff_range with an empty range (a sha
        # compared against itself produces no diff). If validate-comments
        # uses the stored value, feat.py will be absent from the diff and
        # the comment will be dismissed as a fabrication. If it recomputes,
        # the comment will survive because feat.py IS on the feature branch.
        empty_range = f"{feat_sha}..{feat_sha}"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE code_reviews SET diff_range = ? WHERE id = ?",
                (empty_range, review_id),
            )
            conn.commit()

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        # Stored range was honoured: feat.py is not in its diff_files set,
        # so the comment got dismissed.
        assert payload["range"] == empty_range, (
            f"validate-comments must report the stored range, got "
            f"{payload['range']!r}"
        )
        assert "feat.py" not in payload["diff_files"], (
            f"feat.py must not be in diff_files when using the empty stored "
            f"range, got {payload['diff_files']}"
        )
        assert len(payload["dismissed"]) == 1
        assert payload["dismissed"][0]["file_path"] == "feat.py"

    def test_validate_comments_falls_back_when_stored_range_null(
        self, tmp_path, monkeypatch
    ):
        """Back-compat for pre-v69 rows: when ``diff_range IS NULL``,
        ``validate-comments`` falls back to ``compute_range`` so historical
        reviews keep working without a backfill."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)
        _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], cwd=repo)
        (repo / "kept.py").write_text("k\n", encoding="utf-8")
        _git(["add", "kept.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] kept"], cwd=repo, env=env)

        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        r = _run(
            ["review", "add-comment", str(review_id), "issue on kept",
             "--file", "kept.py", "--line-start", "1",
             "--category", "suggest", "--severity", "minor"],
            cwd=repo, env=env,
        )
        assert r.returncode == 0, r.stderr

        # Simulate a pre-migration row by NULL-ing diff_range.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE code_reviews SET diff_range = NULL WHERE id = ?",
                (review_id,),
            )
            conn.commit()

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        # The recomputed range covers kept.py — comment survives.
        assert "kept.py" in payload["diff_files"]
        assert payload["dismissed"] == []
        assert payload["in_diff"] == 1

    def test_review_begin_stamps_concrete_sha_not_symbolic_head_857(
        self, tmp_path, monkeypatch
    ):
        """Issue #857: the stamped ``diff_range`` for the primary path must
        be the concrete ``origin/<base>...<sha>`` form, not the symbolic
        ``origin/<base>...HEAD``. The symbolic form drifts when validate is
        invoked from a different cwd than begin (HEAD resolves against the
        validator's cwd, not the begin-time cwd). The concrete SHA form is
        cwd-independent thanks to the shared git object database. Recovery
        path's ``<sha>^..<sha>`` shape is already concrete and is verified
        separately by the unit tests in TestTaskCommitRecovery."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)
        _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], cwd=repo)
        (repo / "feat.py").write_text("x\n", encoding="utf-8")
        _git(["add", "feat.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] feat"], cwd=repo, env=env)
        begin_head_sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT diff_range FROM code_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
        stored = row[0]
        assert not stored.endswith("...HEAD"), (
            f"stamped diff_range must be concretized; got {stored!r}"
        )
        assert stored.endswith(f"...{begin_head_sha}"), (
            f"stamped diff_range must end with begin-time HEAD SHA "
            f"{begin_head_sha!r}; got {stored!r}"
        )

    def test_validate_comments_unaffected_by_post_begin_head_movement_857(
        self, tmp_path, monkeypatch
    ):
        """Issue #857 end-to-end: the consequence of concretizing the
        stamped range is that HEAD movement between ``tusk review begin``
        and ``tusk review validate-comments`` does not change which files
        the validator considers in-diff. Without the fix, the stamped
        ``origin/main...HEAD`` would re-resolve at validate time and pick
        up the post-begin commit, contaminating the file list."""
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)
        _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], cwd=repo)
        (repo / "feat.py").write_text("x\n", encoding="utf-8")
        _git(["add", "feat.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] feat"], cwd=repo, env=env)

        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]

        # Add a comment on feat.py — the file from the begin-time HEAD.
        r = _run(
            ["review", "add-comment", str(review_id), "issue on feat",
             "--file", "feat.py", "--line-start", "1",
             "--category", "must_fix", "--severity", "minor"],
            cwd=repo, env=env,
        )
        assert r.returncode == 0, r.stderr

        # Advance HEAD on the same checkout with an unrelated commit.
        # Under the symbolic-HEAD bug, the validator would re-resolve
        # ...HEAD to this new commit and include unrelated.txt in
        # diff_files. With concretization, the stored SHA pins to the
        # begin-time HEAD and unrelated.txt stays out.
        (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        _git(["add", "unrelated.txt"], cwd=repo, env=env)
        _git(["commit", "-m", "unrelated post-begin commit"], cwd=repo, env=env)

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        # The post-begin commit's file must not be in diff_files — the
        # stored range pins to the begin-time HEAD.
        assert "unrelated.txt" not in payload["diff_files"], (
            f"unrelated.txt from post-begin commit must not contaminate "
            f"diff_files when the stored range is concrete; "
            f"got {payload['diff_files']}"
        )
        # The feat.py comment must survive (in-diff under the stamped range).
        assert "feat.py" in payload["diff_files"]
        assert payload["dismissed"] == []
        assert payload["in_diff"] == 1


class TestValidateCommentsGeneralBodyScan:
    """Issue #912: general comments (null file_path) are body-scanned for
    file-path-shaped tokens. A general comment whose cited paths are all
    absent from the diff is dismissed under the same fabrication-guard
    rationale; one that cites at least one in-diff path or no path tokens
    at all is preserved."""

    def _setup_real_branch(self, tmp_path, monkeypatch):
        repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
        task_id = _insert_in_progress_task(db_path)
        _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], cwd=repo)
        (repo / "real.py").write_text("def real():\n    return 1\n", encoding="utf-8")
        _git(["add", "real.py"], cwd=repo, env=env)
        _git(["commit", "-m", f"[TASK-{task_id}] real"], cwd=repo, env=env)
        begin = _run(["review", "begin", str(task_id)], cwd=repo, env=env)
        assert begin.returncode == 0, begin.stderr
        review_id = json.loads(begin.stdout)["review_id"]
        return repo, db_path, env, task_id, review_id

    def test_general_comment_with_out_of_diff_path_is_dismissed(
        self, tmp_path, monkeypatch
    ):
        """Issue #912 reproduction: a general comment naming
        ``apps/foo/nonexistent.py`` when the diff has only ``real.py`` is
        auto-dismissed via the body-scan branch."""
        repo, db_path, env, _task_id, review_id = self._setup_real_branch(
            tmp_path, monkeypatch
        )
        r = _run(
            ["review", "add-comment", str(review_id),
             "Scope: this branch also bundles an unrelated change to apps/foo/nonexistent.py",
             "--category", "suggest", "--severity", "minor"],
            cwd=repo, env=env,
        )
        assert r.returncode == 0, r.stderr

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["dismissed"] == []
        assert payload["general"] == 0, (
            "the general comment got dismissed, not preserved"
        )
        assert len(payload["dismissed_general"]) == 1
        entry = payload["dismissed_general"][0]
        assert entry["cited_paths"] == ["apps/foo/nonexistent.py"]

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT resolution, resolution_note FROM review_comments"
                " WHERE id = ?",
                (entry["comment_id"],),
            ).fetchone()
        assert row["resolution"] == "dismissed"
        assert "apps/foo/nonexistent.py" in (row["resolution_note"] or "")
        assert "issue #912" in (row["resolution_note"] or "")

    def test_general_comment_citing_in_diff_path_is_preserved(
        self, tmp_path, monkeypatch
    ):
        repo, db_path, env, _task_id, review_id = self._setup_real_branch(
            tmp_path, monkeypatch
        )
        r = _run(
            ["review", "add-comment", str(review_id),
             "Scope concern: the change to real.py needs broader review",
             "--category", "suggest", "--severity", "minor"],
            cwd=repo, env=env,
        )
        assert r.returncode == 0, r.stderr

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["dismissed_general"] == [], (
            "general comment citing an in-diff path must NOT be dismissed"
        )
        assert payload["general"] == 1

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT resolution FROM review_comments WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        assert row["resolution"] is None

    def test_general_comment_citing_no_paths_is_preserved(
        self, tmp_path, monkeypatch
    ):
        """Truly generic remarks with no file-path-shaped tokens must
        still be preserved — the orchestrator's diff-line-quote rule
        handles those at the per-comment loop."""
        repo, db_path, env, _task_id, review_id = self._setup_real_branch(
            tmp_path, monkeypatch
        )
        r = _run(
            ["review", "add-comment", str(review_id),
             "Overall the diff feels OK; consider tightening error messages.",
             "--category", "suggest", "--severity", "minor"],
            cwd=repo, env=env,
        )
        assert r.returncode == 0, r.stderr

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["dismissed_general"] == []
        assert payload["general"] == 1

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT resolution FROM review_comments WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        assert row["resolution"] is None

    def test_general_comment_with_mixed_in_and_out_of_diff_paths_is_preserved(
        self, tmp_path, monkeypatch
    ):
        """Mixed citations: if at least one cited path IS in the diff,
        the comment is preserved — partial overlap is enough to clear
        the fabrication-guard bar."""
        repo, db_path, env, _task_id, review_id = self._setup_real_branch(
            tmp_path, monkeypatch
        )
        r = _run(
            ["review", "add-comment", str(review_id),
             "real.py is fine but also touch apps/foo/nonexistent.py",
             "--category", "suggest", "--severity", "minor"],
            cwd=repo, env=env,
        )
        assert r.returncode == 0, r.stderr

        result = _run(["review", "validate-comments", str(review_id)], cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["dismissed_general"] == []
        assert payload["general"] == 1

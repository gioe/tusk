"""Unit tests for tusk-sync-main.py.

The integration-test surface for this helper is a real git repository with
two commits ahead of a remote — which is awkward to fixture in CI without
network. These tests fault-inject the underlying ``_run`` subprocess wrapper
to exercise the stash-by-ref invariants:

- ``_find_stash_ref`` raises on git failure (refusing the silent-data-loss
  conflation that test-precheck's pattern also enforces).
- The dirty-tree path creates a uniquely-named stash, pops by ref, and
  reports ``stashed=True`` in the JSON payload.
- The clean-tree path never touches ``git stash`` at all.
- The "already up to date" path (``fetched_commits == 0``) skips the
  ff-merge AND the stash entirely, but still runs ``tusk migrate``.
"""

import importlib.util
import os
import subprocess
import sys
from unittest import mock

import pytest


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_sync_main",
    os.path.join(BIN, "tusk-sync-main.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _ok(stdout=""):
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _err(stderr="boom"):
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr
    )


class TestFindStashRef:
    def test_raises_when_git_stash_list_exits_nonzero(self):
        with mock.patch.object(mod, "_run", return_value=_err("not a git repo")):
            with pytest.raises(RuntimeError, match="git stash list failed"):
                mod._find_stash_ref("/tmp/whatever", "msg")

    def test_returns_empty_only_when_no_match(self):
        fake = _ok(stdout="stash@{0} On main: someone else\n")
        with mock.patch.object(mod, "_run", return_value=fake):
            assert mod._find_stash_ref("/tmp/whatever", "ours") == ""

    def test_returns_ref_when_match_exists_anywhere_in_stack(self):
        fake = _ok(
            stdout=(
                "stash@{0} On main: intruder\n"
                "stash@{1} On main: tusk-sync-main/123/abc\n"
                "stash@{2} On main: older\n"
            )
        )
        with mock.patch.object(mod, "_run", return_value=fake):
            assert (
                mod._find_stash_ref("/tmp/whatever", "tusk-sync-main/123/abc")
                == "stash@{1}"
            )


class TestSyncMainStashFlow:
    """The flow uses ``_run`` for every git/tusk invocation. We script a
    response queue keyed on the first two argv tokens — that captures the
    sequence the script actually walks without coupling to argv positions.
    """

    def _scripted(self, plan):
        """Return a fake ``_run`` that pops responses from *plan* in order.

        Each entry in *plan* is a tuple ``(matcher, response)`` where
        *matcher* is a callable receiving the argv list and returning
        bool, and *response* is the ``CompletedProcess`` to return.
        """
        calls = []

        def fake_run(cmd, cwd, check=False, env=None):
            calls.append(list(cmd))
            for matcher, response in plan:
                if matcher(cmd):
                    return response
            raise AssertionError(f"unexpected _run call: {cmd}")

        return fake_run, calls

    def test_clean_tree_skips_stash_and_pop(self):
        """No stash push, no stash list, no pop when working tree is clean."""
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (
                lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c,
                _ok(""),
            ),
            (lambda c: c[:2] == ["git", "fetch"], _ok("")),
            (lambda c: c[:2] == ["git", "rev-list"], _ok("2\n")),
            (lambda c: c[:2] == ["git", "status"], _ok("")),  # clean
            (lambda c: c[:3] == ["git", "merge", "--ff-only"], _ok("")),
            (lambda c: c[-1] == "migrate", _ok("")),
        ]
        fake_run, calls = self._scripted(plan)
        with mock.patch.object(mod, "_run", side_effect=fake_run):
            code, payload = mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        assert code == 0
        assert payload["success"] is True
        assert payload["stashed"] is False
        assert payload["fetched_commits"] == 2
        assert payload["migrated"] is True
        assert not any(c[:2] == ["git", "stash"] for c in calls)

    def test_dirty_tree_stashes_pops_by_ref(self):
        """Dirty tree → stash push, find by message, ff-merge, find again, pop."""
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (
                lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c,
                _ok(""),
            ),
            (lambda c: c[:2] == ["git", "fetch"], _ok("")),
            (lambda c: c[:2] == ["git", "rev-list"], _ok("5\n")),
            (lambda c: c[:2] == ["git", "status"], _ok(" M file.py\n")),
            # Pre-flight (issue #1095): temp-index build + clean merge-tree.
            (lambda c: c[:2] == ["git", "read-tree"], _ok("")),
            (lambda c: c[:2] == ["git", "add"], _ok("")),
            (lambda c: c[:2] == ["git", "write-tree"], _ok("treeoid\n")),
            (lambda c: c[:2] == ["git", "commit-tree"], _ok("commitoid\n")),
            (lambda c: c[:2] == ["git", "merge-tree"], _ok("treeoid\n")),
            (lambda c: c[:3] == ["git", "stash", "push"], _ok("")),
            # find_stash_ref returns our entry (called twice — pre-merge + post-merge)
            (
                lambda c: c[:3] == ["git", "stash", "list"],
                _ok("stash@{0} On main: tusk-sync-main/12345/abcdef00\n"),
            ),
            (lambda c: c[:3] == ["git", "merge", "--ff-only"], _ok("")),
            (
                lambda c: c[:3] == ["git", "stash", "list"],
                _ok("stash@{0} On main: tusk-sync-main/12345/abcdef00\n"),
            ),
            (lambda c: c[:3] == ["git", "stash", "pop"], _ok("")),
            (lambda c: c[-1] == "migrate", _ok("")),
        ]
        fake_run, calls = self._scripted(plan)
        # Pin the uuid suffix so the stash message is predictable in our matcher.
        with mock.patch.object(mod.uuid, "uuid4", return_value=mock.Mock(hex="abcdef00")):
            with mock.patch.object(mod.os, "getpid", return_value=12345):
                with mock.patch.object(mod, "_run", side_effect=fake_run):
                    code, payload = mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        assert code == 0
        assert payload["success"] is True
        assert payload["stashed"] is True
        assert payload["fetched_commits"] == 5
        # The pop must reference an entry by its looked-up ref, not by position.
        pop_calls = [c for c in calls if c[:3] == ["git", "stash", "pop"]]
        assert pop_calls and pop_calls[0][3].startswith("stash@{")

    def test_unmerged_paths_short_circuits_before_fetch(self):
        """UU paths → exit 1 with diagnostic naming the file; never fetch/stash/migrate (issue #914)."""
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (
                lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c,
                _ok("a.txt\n"),
            ),
        ]
        fake_run, calls = self._scripted(plan)
        with mock.patch.object(mod, "_run", side_effect=fake_run):
            code, payload = mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        assert code == 1
        assert payload["success"] is False
        assert payload["default_branch"] == "main"
        assert payload["fetched_commits"] == 0
        assert payload["stashed"] is False
        assert payload["migrated"] is False
        # State-mutating steps must NOT have run.
        assert not any(c[:2] == ["git", "fetch"] for c in calls)
        assert not any(c[:2] == ["git", "stash"] for c in calls)
        assert not any(c[:3] == ["git", "merge", "--ff-only"] for c in calls)
        assert not any(c[-1] == "migrate" for c in calls)

    def test_unmerged_paths_diagnostic_names_every_file(self, capsys):
        """Diagnostic surfaces every unmerged path, not just a count."""
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (
                lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c,
                _ok("a.txt\nsubdir/b.md\nthird.json\n"),
            ),
        ]
        fake_run, _ = self._scripted(plan)
        with mock.patch.object(mod, "_run", side_effect=fake_run):
            mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        captured = capsys.readouterr()
        assert "unmerged" in captured.err.lower()
        assert "3 unmerged path(s)" in captured.err
        for path in ("a.txt", "subdir/b.md", "third.json"):
            assert path in captured.err
        assert "resolve them before tusk sync-main" in captured.err

    def test_unmerged_paths_diagnostic_caps_long_lists(self, capsys):
        """When >10 unmerged paths, diagnostic shows the first 10 plus an overflow count."""
        files = [f"f{i:02d}.txt" for i in range(15)]
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (
                lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c,
                _ok("\n".join(files) + "\n"),
            ),
        ]
        fake_run, _ = self._scripted(plan)
        with mock.patch.object(mod, "_run", side_effect=fake_run):
            mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        captured = capsys.readouterr()
        assert "15 unmerged path(s)" in captured.err
        # First 10 named, last 5 collapsed into an overflow count.
        for path in files[:10]:
            assert path in captured.err
        assert "and 5 more" in captured.err
        for path in files[10:]:
            assert path not in captured.err

    def test_clean_tree_runs_diff_check_then_proceeds(self):
        """Empty unmerged-paths list must NOT short-circuit the normal flow."""
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (
                lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c,
                _ok(""),  # no unmerged paths
            ),
            (lambda c: c[:2] == ["git", "fetch"], _ok("")),
            (lambda c: c[:2] == ["git", "rev-list"], _ok("0\n")),
            (lambda c: c[:2] == ["git", "status"], _ok("")),
            (lambda c: c[-1] == "migrate", _ok("")),
        ]
        fake_run, calls = self._scripted(plan)
        with mock.patch.object(mod, "_run", side_effect=fake_run):
            code, payload = mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        assert code == 0
        assert payload["success"] is True
        # The fetch + migrate path was actually exercised.
        assert any(c[:2] == ["git", "fetch"] for c in calls)
        assert any(c[-1] == "migrate" for c in calls)

    def test_already_up_to_date_skips_merge_and_stash(self):
        """fetched_commits == 0 → skip ff-merge AND stash, but still migrate."""
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (
                lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c,
                _ok(""),
            ),
            (lambda c: c[:2] == ["git", "fetch"], _ok("")),
            (lambda c: c[:2] == ["git", "rev-list"], _ok("0\n")),
            (lambda c: c[:2] == ["git", "status"], _ok(" M file.py\n")),  # dirty
            (lambda c: c[-1] == "migrate", _ok("")),
        ]
        fake_run, calls = self._scripted(plan)
        with mock.patch.object(mod, "_run", side_effect=fake_run):
            code, payload = mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        assert code == 0
        assert payload["success"] is True
        assert payload["stashed"] is False  # didn't bother — nothing to ff over
        assert payload["fetched_commits"] == 0
        assert payload["migrated"] is True
        # No stash push, no ff-merge.
        assert not any(c[:2] == ["git", "stash"] for c in calls)
        assert not any(c[:3] == ["git", "merge", "--ff-only"] for c in calls)

    def test_preflight_conflict_aborts_before_stash(self, capsys):
        """A pre-flight conflict aborts before stashing, leaving the tree
        untouched (no stash push, no ff-merge, no migrate) and names the
        conflicted file(s) (issue #1095)."""
        merge_conflict = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=(
                "treeoid\n"
                "100644 aaa 1\tfile.py\n"
                "100644 bbb 2\tfile.py\n"
                "\n"
                "CONFLICT (content): Merge conflict in file.py\n"
            ),
            stderr="",
        )
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c, _ok("")),
            (lambda c: c[:2] == ["git", "fetch"], _ok("")),
            (lambda c: c[:2] == ["git", "rev-list"], _ok("3\n")),
            (lambda c: c[:2] == ["git", "status"], _ok(" M file.py\n")),
            (lambda c: c[:2] == ["git", "read-tree"], _ok("")),
            (lambda c: c[:2] == ["git", "add"], _ok("")),
            (lambda c: c[:2] == ["git", "write-tree"], _ok("treeoid\n")),
            (lambda c: c[:2] == ["git", "commit-tree"], _ok("commitoid\n")),
            (lambda c: c[:2] == ["git", "merge-tree"], merge_conflict),
        ]
        fake_run, calls = self._scripted(plan)
        with mock.patch.object(mod, "_run", side_effect=fake_run):
            code, payload = mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        assert code == 1
        assert payload["success"] is False
        assert payload["stashed"] is False
        err = capsys.readouterr().err
        assert "would conflict" in err
        assert "file.py" in err
        # Working tree left untouched: no stash push, no ff-merge, no migrate.
        assert not any(c[:3] == ["git", "stash", "push"] for c in calls)
        assert not any(c[:3] == ["git", "merge", "--ff-only"] for c in calls)
        assert not any(c[-1] == "migrate" for c in calls)

    def test_preflight_disabled_by_env_skips_check(self):
        """TUSK_SYNC_MAIN_NO_PREFLIGHT=1 bypasses the pre-flight entirely —
        no merge-tree probe runs, the normal stash/pop path proceeds."""
        plan = [
            (lambda c: c[:2] == ["git", "symbolic-ref"], _ok("origin/main\n")),
            (lambda c: c[:2] == ["git", "diff"] and "--diff-filter=U" in c, _ok("")),
            (lambda c: c[:2] == ["git", "fetch"], _ok("")),
            (lambda c: c[:2] == ["git", "rev-list"], _ok("5\n")),
            (lambda c: c[:2] == ["git", "status"], _ok(" M file.py\n")),
            (lambda c: c[:3] == ["git", "stash", "push"], _ok("")),
            (
                lambda c: c[:3] == ["git", "stash", "list"],
                _ok("stash@{0} On main: tusk-sync-main/12345/abcdef00\n"),
            ),
            (lambda c: c[:3] == ["git", "merge", "--ff-only"], _ok("")),
            (lambda c: c[:3] == ["git", "stash", "pop"], _ok("")),
            (lambda c: c[-1] == "migrate", _ok("")),
        ]
        fake_run, calls = self._scripted(plan)
        with mock.patch.dict(mod.os.environ, {"TUSK_SYNC_MAIN_NO_PREFLIGHT": "1"}):
            with mock.patch.object(
                mod.uuid, "uuid4", return_value=mock.Mock(hex="abcdef00")
            ), mock.patch.object(mod.os, "getpid", return_value=12345), \
                    mock.patch.object(mod, "_run", side_effect=fake_run):
                code, payload = mod.sync_main("/tmp/repo", "/tmp/bin/tusk")
        assert code == 0
        assert payload["stashed"] is True
        assert not any(c[:2] == ["git", "merge-tree"] for c in calls)


class TestPreflightStashConflict:
    """``_preflight_stash_conflict`` 3-way merge simulation (issue #1095)."""

    def _fake(self, merge_result):
        def fake_run(cmd, cwd, check=False, env=None):
            if cmd[:2] == ["git", "read-tree"]:
                return _ok("")
            if cmd[:2] == ["git", "add"]:
                return _ok("")
            if cmd[:2] == ["git", "write-tree"]:
                return _ok("treeoid\n")
            if cmd[:2] == ["git", "commit-tree"]:
                return _ok("commitoid\n")
            if cmd[:2] == ["git", "merge-tree"]:
                return merge_result
            raise AssertionError(f"unexpected _run call: {cmd}")

        return fake_run

    def test_conflict_returns_paths(self):
        merge = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=(
                "treeoid\n"
                "100644 aaa 1\tsrc/x.py\n"
                "100644 bbb 2\tsrc/x.py\n"
                "\n"
                "CONFLICT (content): Merge conflict in src/x.py\n"
            ),
            stderr="",
        )
        with mock.patch.object(mod, "_run", side_effect=self._fake(merge)):
            assert mod._preflight_stash_conflict("/repo", "main") == ["src/x.py"]

    def test_clean_returns_empty_list(self):
        with mock.patch.object(mod, "_run", side_effect=self._fake(_ok("treeoid\n"))):
            assert mod._preflight_stash_conflict("/repo", "main") == []

    def test_indeterminate_rc_returns_none(self):
        merge = subprocess.CompletedProcess(
            args=[], returncode=129, stdout="", stderr="usage: git merge-tree"
        )
        with mock.patch.object(mod, "_run", side_effect=self._fake(merge)):
            assert mod._preflight_stash_conflict("/repo", "main") is None

    def test_write_tree_failure_returns_none(self):
        def fake_run(cmd, cwd, check=False, env=None):
            if cmd[:2] == ["git", "read-tree"]:
                return _ok("")
            if cmd[:2] == ["git", "add"]:
                return _ok("")
            if cmd[:2] == ["git", "write-tree"]:
                return _err("boom")
            raise AssertionError(f"unexpected _run call: {cmd}")

        with mock.patch.object(mod, "_run", side_effect=fake_run):
            assert mod._preflight_stash_conflict("/repo", "main") is None


class TestParseMergeTreeConflicts:
    """Parsing ``git merge-tree --write-tree`` conflict output (issue #1095)."""

    def test_extracts_unique_paths_until_blank_line(self):
        out = (
            "treeoid\n"
            "100644 a 1\tsrc/x.py\n"
            "100644 b 2\tsrc/x.py\n"
            "100644 c 1\tsrc/y.py\n"
            "\n"
            "CONFLICT (content): Merge conflict in src/x.py\n"
        )
        assert mod._parse_merge_tree_conflicts(out) == ["src/x.py", "src/y.py"]

    def test_clean_output_has_no_conflict_paths(self):
        assert mod._parse_merge_tree_conflicts("treeoid\n") == []


class TestPopStashWithLockRetry:
    """Transient index.lock contention retry on the pop (issue #1075)."""

    LOCK_STDERR = "error: could not write index"
    LOCK_STDERR_CREATE = (
        "fatal: Unable to create '/repo/.git/index.lock': File exists."
    )
    CONFLICT_STDERR = (
        "CONFLICT (content): Merge conflict in file.py\n"
        "The stash entry is kept in case you need it again."
    )

    def test_retry_succeeds_after_transient_lock(self, capsys):
        results = [_err(self.LOCK_STDERR), _ok("")]
        calls = []

        def fake_run(cmd, cwd, check=False):
            calls.append(cmd)
            return results.pop(0)

        with mock.patch.object(mod, "_run", side_effect=fake_run), \
                mock.patch.object(mod.time, "sleep") as sleep_mock:
            res = mod._pop_stash_with_lock_retry("/tmp/repo", "stash@{0}")

        assert res.returncode == 0
        assert len(calls) == 2
        assert all(c[:3] == ["git", "stash", "pop"] for c in calls)
        sleep_mock.assert_called_once()
        assert "transient .git/index.lock contention" in capsys.readouterr().err

    def test_unable_to_create_lock_also_retried(self):
        results = [_err(self.LOCK_STDERR_CREATE), _ok("")]
        with mock.patch.object(mod, "_run", side_effect=lambda *a, **k: results.pop(0)), \
                mock.patch.object(mod.time, "sleep"):
            res = mod._pop_stash_with_lock_retry("/tmp/repo", "stash@{0}")
        assert res.returncode == 0

    def test_retries_exhausted_returns_last_failure(self, capsys):
        attempts = 1 + len(mod._POP_LOCK_BACKOFF_SECONDS)
        results = [_err(self.LOCK_STDERR) for _ in range(attempts)]
        calls = []

        def fake_run(cmd, cwd, check=False):
            calls.append(cmd)
            return results.pop(0)

        with mock.patch.object(mod, "_run", side_effect=fake_run), \
                mock.patch.object(mod.time, "sleep") as sleep_mock:
            res = mod._pop_stash_with_lock_retry("/tmp/repo", "stash@{0}")

        assert res.returncode == 1
        assert len(calls) == attempts
        assert sleep_mock.call_count == len(mod._POP_LOCK_BACKOFF_SECONDS)

    def test_conflict_is_not_retried(self, capsys):
        calls = []

        def fake_run(cmd, cwd, check=False):
            calls.append(cmd)
            return _err(self.CONFLICT_STDERR)

        with mock.patch.object(mod, "_run", side_effect=fake_run), \
                mock.patch.object(mod.time, "sleep") as sleep_mock:
            res = mod._pop_stash_with_lock_retry("/tmp/repo", "stash@{0}")

        assert res.returncode == 1
        assert len(calls) == 1
        sleep_mock.assert_not_called()
        assert "retrying" not in capsys.readouterr().err

    def test_sync_main_pop_failure_keeps_recovery_message(self, capsys):
        """End-to-end: a non-retryable pop failure still names the stash entry."""
        pushed_message = {}

        def fake_run(cmd, cwd, check=False, env=None):
            if cmd[:2] == ["git", "symbolic-ref"]:
                return _ok("refs/remotes/origin/main\n")
            if cmd[:2] == ["git", "diff"]:
                return _ok("")
            if cmd[:2] == ["git", "fetch"]:
                return _ok("")
            if cmd[:2] == ["git", "rev-list"]:
                return _ok("2\n")
            if cmd[:2] == ["git", "status"]:
                return _ok(" M file.py\n")
            # Pre-flight (issue #1095) passes clean so the pop-conflict path
            # below is still exercised — pre-flight is best-effort and cannot
            # catch every residual pop conflict.
            if cmd[:2] == ["git", "read-tree"]:
                return _ok("")
            if cmd[:2] == ["git", "add"]:
                return _ok("")
            if cmd[:2] == ["git", "write-tree"]:
                return _ok("treeoid\n")
            if cmd[:2] == ["git", "commit-tree"]:
                return _ok("commitoid\n")
            if cmd[:2] == ["git", "merge-tree"]:
                return _ok("treeoid\n")
            if cmd[:3] == ["git", "stash", "push"]:
                pushed_message["msg"] = cmd[cmd.index("-m") + 1]
                return _ok("")
            if cmd[:3] == ["git", "stash", "list"]:
                msg = pushed_message.get("msg", "")
                return _ok(f"stash@{{0}}: On main: {msg}\n")
            if cmd[:3] == ["git", "merge", "--ff-only"]:
                return _ok("")
            if cmd[:3] == ["git", "stash", "pop"]:
                return _err(self.CONFLICT_STDERR)
            return _ok("")

        with mock.patch.object(mod, "_run", side_effect=fake_run), \
                mock.patch.object(mod.time, "sleep"):
            code, payload = mod.sync_main("/tmp/repo", "/tmp/bin/tusk")

        assert code == 1
        assert payload["success"] is False
        err = capsys.readouterr().err
        # Conflicted pops route to the structured recovery (issue #1063).
        assert "hit a merge conflict and PARTIALLY applied" in err
        assert "git reset" in err
        assert "git stash drop" in err


class TestFormatPopFailure:
    """Conflicted-pop recovery guidance (issue #1063)."""

    def _pop_res(self, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout=stdout, stderr=stderr
        )

    def test_conflict_names_unmerged_files(self):
        res = self._pop_res(stdout="CONFLICT (content): Merge conflict in app/main.py\n")
        with mock.patch.object(mod, "_unmerged_paths", return_value=["app/main.py"]):
            msg = mod._format_pop_failure("/tmp/repo", "stash@{0}", "tusk-sync-main/1/ab", res)
        assert "hit a merge conflict and PARTIALLY applied" in msg
        assert "Conflicted file(s): app/main.py" in msg
        assert "1. Resolve the conflict markers" in msg
        assert "2. git reset" in msg
        assert "3. git stash drop stash@{0}" in msg
        assert "tusk-sync-main/1/ab" in msg

    def test_conflict_in_stderr_also_detected(self):
        res = self._pop_res(stderr="CONFLICT (content): Merge conflict in x\n")
        with mock.patch.object(mod, "_unmerged_paths", return_value=[]):
            msg = mod._format_pop_failure("/tmp/repo", "stash@{0}", "m", res)
        assert "PARTIALLY applied" in msg
        assert "see `git status` (UU entries)" in msg

    def test_unmerged_paths_failure_falls_back_to_git_status_hint(self):
        res = self._pop_res(stdout="CONFLICT (content): Merge conflict in x\n")
        with mock.patch.object(
            mod, "_unmerged_paths", side_effect=RuntimeError("boom")
        ):
            msg = mod._format_pop_failure("/tmp/repo", "stash@{0}", "m", res)
        assert "see `git status` (UU entries)" in msg

    def test_non_conflict_keeps_generic_message(self):
        res = self._pop_res(stderr="error: could not write index")
        msg = mod._format_pop_failure("/tmp/repo", "stash@{0}", "tusk-sync-main/1/ab", res)
        assert "git stash pop stash@{0} failed" in msg
        assert "Your changes remain in the stash list" in msg
        assert "PARTIALLY applied" not in msg

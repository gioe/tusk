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

        def fake_run(cmd, cwd, check=False):
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

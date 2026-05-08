"""Unit tests for tusk-branch.py's auto-stash residue warning (issue #671).

When `tusk branch <id>` is about to push a new auto-stash, count the existing
`tusk-branch: auto-stash for TASK-N` entries already in `git stash list`. If the
count exceeds BRANCH_AUTOSTASH_WARN_THRESHOLD, print a warning to stderr listing
each entry alongside the referenced task's current status — so the operator can
decide whether to drop accumulating orphans before adding another.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRANCH_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-branch.py")
GIT_HELPERS_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-git-helpers.py")


def _load_git_helpers():
    spec = importlib.util.spec_from_file_location("tusk_git_helpers", GIT_HELPERS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    db_lib_mock.get_connection = MagicMock()

    def load(name):
        if name == "tusk-git-helpers":
            return _load_git_helpers()
        return db_lib_mock

    tusk_loader_mock.load.side_effect = load
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_branch", BRANCH_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _cp(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _make_stash_list_stdout(branch_task_ids: list[int], extras: list[str] | None = None) -> str:
    """Build a fake `git stash list` payload.

    Each ID in *branch_task_ids* becomes a `tusk-branch: auto-stash for TASK-N`
    line at sequential `stash@{i}` indices. *extras* are interleaved as raw
    lines (e.g. tusk-merge: entries, unrelated work) — they consume their own
    indices but are not parsed as branch-stash entries.
    """
    extras = extras or []
    lines = []
    idx = 0
    for tid in branch_task_ids:
        lines.append(f"stash@{{{idx}}}: On main: tusk-branch: auto-stash for TASK-{tid}")
        idx += 1
    for extra in extras:
        lines.append(f"stash@{{{idx}}}: On main: {extra}")
        idx += 1
    return "\n".join(lines) + "\n" if lines else ""


class TestThresholdBoundary:
    """The warning fires only when count exceeds BRANCH_AUTOSTASH_WARN_THRESHOLD."""

    def test_silent_at_threshold(self, capsys):
        mod = _load_module()
        # Exactly threshold entries — no warning.
        ids = list(range(101, 101 + mod.BRANCH_AUTOSTASH_WARN_THRESHOLD))
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(0)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(0, stdout=_make_stash_list_stdout(ids))
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash_residue("/repo")

        err = capsys.readouterr().err
        assert err == ""

    def test_warns_above_threshold(self, capsys):
        mod = _load_module()
        ids = list(range(201, 201 + mod.BRANCH_AUTOSTASH_WARN_THRESHOLD + 1))

        def fake_run(args, check=True):
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(0)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(0, stdout=_make_stash_list_stdout(ids))
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod, "_get_task_statuses", return_value={tid: "Done" for tid in ids}
        ):
            mod._warn_branch_auto_stash_residue("/repo")

        err = capsys.readouterr().err
        assert "accumulating in `git stash list`" in err
        for tid in ids:
            assert f"TASK-{tid}" in err


class TestStatusListing:
    """The warning lists each entry with the referenced task's current status."""

    def test_warning_includes_each_task_status(self, capsys):
        mod = _load_module()
        ids = [301, 302, 303, 304, 305, 306]  # threshold + 1
        statuses = {
            301: "Done",
            302: "In Progress",
            303: "To Do",
            304: "Done",
            305: "Done",
            306: "unknown",
        }

        def fake_run(args, check=True):
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(0)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(0, stdout=_make_stash_list_stdout(ids))
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod, "_get_task_statuses", return_value=statuses
        ):
            mod._warn_branch_auto_stash_residue("/repo")

        err = capsys.readouterr().err
        # Each TASK-N must appear with its referenced status.
        for tid, status in statuses.items():
            assert f"TASK-{tid}" in err
            assert status in err
        # And the stash@{N} index for each entry is present.
        for idx in range(len(ids)):
            assert f"stash@{{{idx}}}" in err

    def test_unknown_status_for_missing_task_id(self, capsys):
        mod = _load_module()
        ids = [401, 402, 403, 404, 405, 999]  # threshold + 1; 999 is not in DB

        def fake_run(args, check=True):
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(0)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(0, stdout=_make_stash_list_stdout(ids))
            return _cp(0)

        # Simulate `_get_task_statuses` returning 'unknown' for IDs not in the
        # tasks table — that is the documented contract.
        statuses = {tid: "Done" for tid in ids[:-1]} | {999: "unknown"}
        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod, "_get_task_statuses", return_value=statuses
        ):
            mod._warn_branch_auto_stash_residue("/repo")

        err = capsys.readouterr().err
        assert "TASK-999" in err
        assert "unknown" in err


class TestFastExit:
    """The cheap-path fast-exit pattern (issue #658) suppresses extra calls."""

    def test_silent_when_no_refs_stash(self, capsys):
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(1)  # refs/stash missing
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash_residue("/repo")

        # No `git stash list` call — fast-exit on missing refs/stash.
        assert not any(c[:3] == ["git", "stash", "list"] for c in calls)
        assert capsys.readouterr().err == ""

    def test_silent_when_stash_list_fails(self, capsys):
        mod = _load_module()

        def fake_run(args, check=True):
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(0)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(1, stderr="fatal: not a git repository")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash_residue("/repo")

        assert capsys.readouterr().err == ""


class TestIgnoresUnrelatedStashEntries:
    """tusk-merge: and free-form stash entries do not count toward the threshold."""

    def test_only_branch_stashes_count(self, capsys):
        mod = _load_module()
        # 3 branch stashes (well under threshold) plus a pile of unrelated
        # entries. Total stash count is high but warning must NOT fire.
        ids = [501, 502, 503]
        extras = [
            "tusk-merge: auto-stash for TASK-99",
            "tusk-merge: auto-stash for TASK-98",
            "WIP on main: 1234abc some message",
            "On main: experimental work",
            "tusk-merge: auto-stash for TASK-97",
            "WIP on main: another",
        ]

        def fake_run(args, check=True):
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(0)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(0, stdout=_make_stash_list_stdout(ids, extras=extras))
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash_residue("/repo")

        # 3 branch entries ≤ threshold of 5 ⇒ no warning, even though stash
        # list has 9 entries total.
        assert capsys.readouterr().err == ""


class TestCleanTreeUnchanged:
    """Existing flow on a clean tree does not invoke the warning helper at all.

    The warning is gated behind `if dirty:` in main(), so a clean tree must not
    trigger any `git stash list` call beyond what the existing flow already
    runs (which is none).
    """

    def test_clean_tree_skips_warning_helper(self, capsys):
        mod = _load_module()
        # Track whether _warn_branch_auto_stash_residue is reached.
        warn_calls: list[tuple] = []

        def fake_warn(*args, **kwargs):
            warn_calls.append((args, kwargs))

        def fake_run(args, check=True):
            if args[:3] == ["git", "remote", "set-head"]:
                return _cp(0)
            if args[:2] == ["git", "symbolic-ref"]:
                return _cp(0, stdout="refs/remotes/origin/main\n")
            if args[:2] == ["git", "status"] and "--porcelain" in args:
                return _cp(0, stdout="")  # clean tree
            if args[:3] == ["git", "remote", "get-url"]:
                return _cp(0)
            if args[:2] == ["git", "checkout"]:
                return _cp(0)
            if args[:2] == ["git", "pull"]:
                return _cp(0)
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod, "_warn_branch_auto_stash_residue", side_effect=fake_warn
        ):
            rc = mod.main([".", "601", "test-slug"])

        assert rc == 0
        assert warn_calls == [], "clean tree must not invoke residue-warning helper"


class TestDirtyTreeIntegrationCallsWarning:
    """When dirty, main() calls the residue warning before stashing."""

    def test_dirty_tree_invokes_warning_before_stash_push(self, capsys):
        mod = _load_module()
        order: list[str] = []

        def fake_warn(repo_root):
            order.append("warn")

        def fake_run(args, check=True):
            if args[:3] == ["git", "remote", "set-head"]:
                return _cp(0)
            if args[:2] == ["git", "symbolic-ref"]:
                return _cp(0, stdout="refs/remotes/origin/main\n")
            if args[:2] == ["git", "status"] and "--porcelain" in args:
                return _cp(0, stdout="M  some_file.py\n")
            if args[:2] == ["git", "stash"] and len(args) > 2 and args[2] == "push":
                order.append("stash_push")
                return _cp(0)
            if args[:3] == ["git", "remote", "get-url"]:
                return _cp(0)
            if args[:2] == ["git", "checkout"]:
                return _cp(0)
            if args[:2] == ["git", "pull"]:
                return _cp(0)
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod, "_warn_branch_auto_stash_residue", side_effect=fake_warn
        ):
            rc = mod.main([".", "701", "test-slug"])

        assert rc == 0
        # Warning must fire BEFORE the stash push so the operator can ctrl-C
        # before the new entry lands.
        assert order == ["warn", "stash_push"]

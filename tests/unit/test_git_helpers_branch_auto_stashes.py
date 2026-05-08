"""Unit tests for shared tusk-branch auto-stash listing helpers."""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GIT_HELPERS_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-git-helpers.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_git_helpers", GIT_HELPERS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestIterBranchAutoStashes:
    def test_yields_branch_auto_stash_indices_and_task_ids_without_prefix_collision(self):
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(0)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(
                    0,
                    stdout=(
                        "stash@{0}: On main: tusk-branch: auto-stash for TASK-42\n"
                        "stash@{1}: On main: tusk-merge: auto-stash for TASK-42\n"
                        "stash@{2}: On main: tusk-branch: auto-stash for TASK-29\n"
                    ),
                )
            return _cp(0)

        with patch.object(mod.subprocess, "run", side_effect=fake_run):
            entries = list(mod.iter_branch_auto_stashes("/repo"))

        assert entries == [(0, 42), (2, 29)]
        assert ["git", "rev-parse", "--verify", "--quiet", "refs/stash"] in calls
        assert ["git", "stash", "list"] in calls

    def test_fast_exits_without_listing_when_refs_stash_is_missing(self):
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return _cp(1)
            return _cp(0)

        with patch.object(mod.subprocess, "run", side_effect=fake_run):
            entries = list(mod.iter_branch_auto_stashes("/repo"))

        assert entries == []
        assert not any(c[:3] == ["git", "stash", "list"] for c in calls)

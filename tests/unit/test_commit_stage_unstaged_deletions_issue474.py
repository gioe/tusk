"""Regression test for GitHub Issue #474.

`tusk commit` must auto-stage unstaged deletions of tracked files (e.g. files
removed via `rm -rf` rather than `git rm`) so they are included in the same
commit as the explicitly-listed files — not left as unstaged changes afterward.

Exercises the Step 2.5 scan in bin/tusk-commit.py: before `git add`, tusk runs
`git ls-files --deleted -z` and appends any results not already in the
user-supplied file list to the set of paths staged by `git add`.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_completed(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _argv(tmp_path, files):
    config = tmp_path / "config.json"
    config.write_text("{}")
    return [str(tmp_path), str(config), "474", "fix thing"] + files


class TestStageUnstagedDeletions:
    """git ls-files --deleted output is appended to the git add pathspec."""

    def test_unstaged_deletions_are_auto_staged(self, tmp_path, capsys):
        """A tracked directory deleted via rm -rf is included in the git add call."""
        mod = _load_module()

        other = tmp_path / "other-file.ts"
        other.write_text("// kept")

        argv = _argv(tmp_path, ["other-file.ts"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:3] == ["git", "ls-files", "--deleted"]:
                # Simulate: some-tracked-dir was rm -rf'd; two files were inside
                return _make_completed(
                    0,
                    stdout="some-tracked-dir/a.txt\x00some-tracked-dir/b.txt\x00",
                )
            if args[:2] == ["git", "add"]:
                captured_add_args.append(list(args))
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main bbb222] fix")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert len(captured_add_args) == 1, (
            f"Expected exactly one git add call, got {len(captured_add_args)}"
        )
        staged = captured_add_args[0]
        assert "other-file.ts" in staged
        assert "some-tracked-dir/a.txt" in staged
        assert "some-tracked-dir/b.txt" in staged

        captured = capsys.readouterr()
        assert "auto-staging 2 unstaged deletion(s)" in captured.out
        assert "some-tracked-dir/a.txt" in captured.out
        assert "some-tracked-dir/b.txt" in captured.out

    def test_no_deletions_leaves_resolved_files_unchanged(self, tmp_path, capsys):
        """When git ls-files --deleted is empty, git add receives only the explicit paths."""
        mod = _load_module()

        other = tmp_path / "other-file.ts"
        other.write_text("// kept")

        argv = _argv(tmp_path, ["other-file.ts"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:3] == ["git", "ls-files", "--deleted"]:
                return _make_completed(0, stdout="")
            if args[:2] == ["git", "add"]:
                captured_add_args.append(list(args))
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main bbb222] fix")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert len(captured_add_args) == 1
        staged = captured_add_args[0]
        # Only the explicitly passed file should be in the add list.
        assert "other-file.ts" in staged
        # Nothing else should be present besides "git", "add", "--", "other-file.ts"
        assert staged == ["git", "add", "--", "other-file.ts"]

        captured = capsys.readouterr()
        assert "auto-staging" not in captured.out

    def test_explicit_deletion_path_not_duplicated(self, tmp_path, capsys):
        """If the user explicitly lists a deleted path, the scan must not double-add it."""
        mod = _load_module()

        other = tmp_path / "other-file.ts"
        other.write_text("// kept")

        # User explicitly lists the deletion (TASK-679 allows deleted paths through pre-flight)
        argv = _argv(tmp_path, ["other-file.ts", "some-tracked-dir/a.txt"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:3] == ["git", "ls-files", "--deleted"]:
                # ls-files reports the already-listed file plus a new one
                return _make_completed(
                    0,
                    stdout="some-tracked-dir/a.txt\x00some-tracked-dir/b.txt\x00",
                )
            if args[:3] == ["git", "ls-files", "--"]:
                # Pre-flight check: user-listed "some-tracked-dir/a.txt" is tracked
                # in the index even though it's not on disk — allow it through.
                return _make_completed(0, stdout="some-tracked-dir/a.txt\n")
            if args[:2] == ["git", "add"]:
                captured_add_args.append(list(args))
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main bbb222] fix")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert len(captured_add_args) == 1
        staged = captured_add_args[0]
        # a.txt must appear exactly once — the scan should dedupe against the user list.
        assert staged.count("some-tracked-dir/a.txt") == 1
        # b.txt was not in the user's list but was detected as a deletion — included.
        assert "some-tracked-dir/b.txt" in staged

        captured = capsys.readouterr()
        # Only one auto-staged deletion (b.txt) — a.txt was already on the user's list.
        assert "auto-staging 1 unstaged deletion(s)" in captured.out

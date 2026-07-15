"""Regression tests for explicit deletion staging (issues #474 and #1212).

`tusk commit` stages a tracked deletion when its path is explicitly listed,
but it must not sweep unrelated working-tree deletions into a coherent commit.
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
    """Only explicitly listed tracked deletions reach the git add pathspec."""

    def test_unrelated_unstaged_deletions_are_not_auto_staged(self, tmp_path, capsys):
        """Tracked deletions outside the explicit path list remain untouched."""
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
        assert staged == ["git", "add", "--", "other-file.ts"]

        captured = capsys.readouterr()
        assert "auto-staging" not in captured.out

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

    def test_explicit_deletion_path_excludes_unrelated_deletion(self, tmp_path, capsys):
        """An explicitly listed deletion is staged without sweeping its sibling."""
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
        # a.txt is explicitly listed and must appear exactly once.
        assert staged.count("some-tracked-dir/a.txt") == 1
        assert "some-tracked-dir/b.txt" not in staged

        captured = capsys.readouterr()
        assert "auto-staging" not in captured.out

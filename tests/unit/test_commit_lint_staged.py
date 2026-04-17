"""Unit tests for tusk-commit.py lint-staged graceful handling.

Verifies two behaviors introduced for GitHub Issue #359:
1. When git add fails with "pathspec did not match" but all files are already
   in the index (pre-staged by lint-staged), tusk commit treats this as a
   no-op and proceeds to commit rather than aborting with exit code 3.
2. When git commit fails and the commit did not land, the error output includes
   a --skip-verify hint so the user knows how to bypass pre-commit hooks.
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


def _argv(tmp_path, files=None):
    config = tmp_path / "config.json"
    config.write_text("{}")
    if files is None:
        (tmp_path / "somefile.py").write_text("")
    return [str(tmp_path), str(config), "42", "my message"] + (files or ["somefile.py"])


class TestAlreadyStagedNoOp:
    """git add fails with 'pathspec did not match' but files ARE in the index."""

    def test_proceeds_to_commit_when_all_files_cached(self, tmp_path):
        """Should exit 0 when git add fails but files are already in the index."""
        mod = _load_module()
        argv = _argv(tmp_path)

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr="fatal: pathspec 'somefile.py' did not match any files",
                )
            if args[:3] == ["git", "ls-files", "--cached"]:
                return _make_completed(0, stdout="somefile.py\n")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main aaa111] msg")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0

    def test_prints_note_when_files_already_staged(self, tmp_path, capsys):
        """Should print an informational note, not an error, when files are pre-staged."""
        mod = _load_module()
        argv = _argv(tmp_path)

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr="fatal: pathspec 'somefile.py' did not match any files",
                )
            if args[:3] == ["git", "ls-files", "--cached"]:
                return _make_completed(0, stdout="somefile.py\n")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main aaa111] msg")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            mod.main(argv)

        captured = capsys.readouterr()
        assert "Note:" in captured.out
        assert "already staged" in captured.out
        assert "Error:" not in captured.err

    def test_returns_3_when_pathspec_and_files_not_cached(self, tmp_path, capsys):
        """Should exit 3 when git add fails and files are NOT in the index."""
        mod = _load_module()
        argv = _argv(tmp_path)

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr="fatal: pathspec 'somefile.py' did not match any files",
                )
            if args[:3] == ["git", "ls-files", "--cached"]:
                return _make_completed(0, stdout="")  # file not in index
            if args[:3] == ["git", "check-ignore", "-v"]:
                return _make_completed(1)  # not ignored
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "Error: git add failed" in captured.err


class TestCommitFailureSkipVerifyHint:
    """git commit fails and commit didn't land — --skip-verify hint must appear."""

    def test_skip_verify_hint_on_generic_commit_failure(self, tmp_path, capsys):
        """A generic commit failure includes a generic --skip-verify hint."""
        mod = _load_module()
        argv = _argv(tmp_path)

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")  # HEAD never advances
            if args[:2] == ["git", "commit"]:
                return _make_completed(1, stderr="error: something went wrong")
            # git diff returns empty → no reformatted files, skip Issue #477 retry.
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "--skip-verify" in captured.err

    def test_targeted_hint_on_lint_staged_failure(self, tmp_path, capsys):
        """A lint-staged commit failure shows a targeted hook hint."""
        mod = _load_module()
        argv = _argv(tmp_path)

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")  # HEAD never advances
            if args[:2] == ["git", "commit"]:
                return _make_completed(
                    1, stderr="lint-staged: Prevented an empty git commit!"
                )
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "--skip-verify" in captured.err
        assert "hook" in captured.err.lower()

    def test_no_verify_passed_to_git_commit_when_skip_verify(self, tmp_path):
        """--skip-verify appends --no-verify to the git commit call."""
        mod = _load_module()
        argv = _argv(tmp_path) + ["--skip-verify"]

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            if cmd[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[branch abc123] msg")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            mod.main(argv)

        commit_call = next((c for c in captured_cmds if c[:2] == ["git", "commit"]), None)
        assert commit_call is not None, "git commit was never called"
        assert "--no-verify" in commit_call

    def test_no_verify_absent_without_skip_verify(self, tmp_path):
        """Without --skip-verify, --no-verify is NOT passed to git commit."""
        mod = _load_module()
        argv = _argv(tmp_path)

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            if cmd[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[branch abc123] msg")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            mod.main(argv)

        commit_call = next((c for c in captured_cmds if c[:2] == ["git", "commit"]), None)
        assert commit_call is not None, "git commit was never called"
        assert "--no-verify" not in commit_call

    def test_hook_landing_still_exits_0(self, tmp_path):
        """Existing behavior: commit lands despite hook non-zero → still exits 0."""
        mod = _load_module()
        argv = _argv(tmp_path)

        head_shas = iter(["aaa111\n", "bbb222\n"])  # HEAD changed → commit landed

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout=next(head_shas))
            if args[:2] == ["git", "commit"]:
                return _make_completed(
                    1, stderr="lint-staged could not find any staged files."
                )
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0

"""Unit tests for tusk-commit.py auto-formatter retry (Issue #477).

Verifies that when a pre-commit hook (black, ruff --fix, prettier, gofmt) rewrites
a staged file in-place, `tusk commit` detects the working-tree/index divergence,
re-stages the reformatted content, and retries `git commit` exactly once.
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


def _argv(tmp_path, task_id="42", message="my message", files=None, extra=None):
    config = tmp_path / "config.json"
    config.write_text("{}")
    if files is None:
        (tmp_path / "somefile.py").write_text("")
    return (
        [str(tmp_path), str(config), task_id, message]
        + (files or ["somefile.py"])
        + (extra or [])
    )


def _make_completed(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestAutoFormatterRetry:
    """Pre-commit hook reformats staged file → tusk commit re-stages and retries."""

    def test_reformat_retry_succeeds(self, tmp_path, capsys):
        """Hook reformat → re-stage → retry commit → success (exit 0)."""
        mod = _load_module()
        argv = _argv(tmp_path)

        side_effects = [
            _make_completed(0),                              # lint
            _make_completed(0, stdout=""),                   # ls-files --deleted (none)
            _make_completed(0),                              # git add (initial)
            _make_completed(0, stdout="aaa111\n"),           # pre HEAD
            _make_completed(
                1,
                stderr="black reformatted somefile.py\npre-commit hook failed",
            ),                                               # git commit — hook rewrote file
            _make_completed(0, stdout="aaa111\n"),           # post HEAD — unchanged
            _make_completed(0, stdout="somefile.py\n"),      # git diff --name-only — reformatted
            _make_completed(0),                              # git add (re-stage)
            _make_completed(0, stdout="[branch bbb222] msg"),# git commit (retry) — success
            _make_completed(0, stdout="bbb222\n"),           # post HEAD (retry) — new SHA
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0, "Should exit 0 when retry succeeds"
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "re-staging reformatted content" in combined
        assert "Error: git commit failed" not in combined

    def test_reformat_retry_fails_returns_3(self, tmp_path, capsys):
        """Hook reformat → re-stage → retry also fails → exit 3, no further loop."""
        mod = _load_module()
        argv = _argv(tmp_path)

        side_effects = [
            _make_completed(0),                              # lint
            _make_completed(0, stdout=""),                   # ls-files --deleted (none)
            _make_completed(0),                              # git add (initial)
            _make_completed(0, stdout="aaa111\n"),           # pre HEAD
            _make_completed(1, stderr="pre-commit hook failed"),
            _make_completed(0, stdout="aaa111\n"),           # post HEAD — unchanged
            _make_completed(0, stdout="somefile.py\n"),      # git diff — reformatted
            _make_completed(0),                              # git add (re-stage)
            _make_completed(1, stderr="pre-commit hook failed again"),
            _make_completed(0, stdout="aaa111\n"),           # post HEAD (retry) — still unchanged
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, "Should exit 3 when retry also fails"
        captured = capsys.readouterr()
        assert "Error: git commit failed" in captured.err
        assert "auto-formatter hook may have rewritten" in captured.err

    def test_no_retry_with_skip_verify(self, tmp_path, capsys):
        """--skip-verify bypasses the retry path entirely (StopIteration proves no extra calls)."""
        mod = _load_module()
        argv = _argv(tmp_path, extra=["--skip-verify"])

        side_effects = [
            _make_completed(0),                              # lint
            _make_completed(0, stdout=""),                   # ls-files --deleted (none)
            _make_completed(0),                              # git add
            _make_completed(0, stdout="aaa111\n"),           # pre HEAD
            _make_completed(1, stderr="something failed"),
            _make_completed(0, stdout="aaa111\n"),           # post HEAD — unchanged
            # If retry path were reached, we'd need a git diff side_effect here.
            # Omitting it asserts the code path is not taken.
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "re-staging reformatted content" not in (captured.out + captured.err)

    def test_no_retry_when_no_reformatted_files(self, tmp_path, capsys):
        """Commit fails but no files were modified by the hook → skip retry, return 3."""
        mod = _load_module()
        argv = _argv(tmp_path)

        side_effects = [
            _make_completed(0),                              # lint
            _make_completed(0, stdout=""),                   # ls-files --deleted (none)
            _make_completed(0),                              # git add
            _make_completed(0, stdout="aaa111\n"),           # pre HEAD
            _make_completed(1, stderr="some hook error"),
            _make_completed(0, stdout="aaa111\n"),           # post HEAD — unchanged
            _make_completed(0, stdout=""),                   # git diff — no reformatted files
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "re-staging reformatted content" not in (captured.out + captured.err)

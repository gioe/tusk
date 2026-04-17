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

        # Initial commit fails with hook-reformat error; retry after re-stage
        # succeeds.  HEAD: pre=aaa111, post-initial=aaa111 (unchanged → triggers
        # retry), post-retry=bbb222 (advanced → commit landed).
        commit_attempts = [0]

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "commit"]:
                commit_attempts[0] += 1
                if commit_attempts[0] == 1:
                    return _make_completed(
                        1,
                        stderr=(
                            "black reformatted somefile.py\npre-commit hook failed"
                        ),
                    )
                return _make_completed(0, stdout="[branch bbb222] msg")
            if args[:2] == ["git", "rev-parse"]:
                # Pre + post-initial both report unchanged HEAD; only the
                # post-retry rev-parse runs after a successful commit.
                sha = "bbb222\n" if commit_attempts[0] >= 2 else "aaa111\n"
                return _make_completed(0, stdout=sha)
            if args[:3] == ["git", "diff", "--name-only"]:
                return _make_completed(0, stdout="somefile.py\n")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
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

        commit_attempts = [0]

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "commit"]:
                commit_attempts[0] += 1
                if commit_attempts[0] == 1:
                    return _make_completed(1, stderr="pre-commit hook failed")
                return _make_completed(1, stderr="pre-commit hook failed again")
            if args[:2] == ["git", "rev-parse"]:
                # HEAD never advances — every rev-parse returns aaa111.
                return _make_completed(0, stdout="aaa111\n")
            if args[:3] == ["git", "diff", "--name-only"]:
                return _make_completed(0, stdout="somefile.py\n")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, "Should exit 3 when retry also fails"
        captured = capsys.readouterr()
        assert "Error: git commit failed" in captured.err
        assert "auto-formatter hook may have rewritten" in captured.err

    def test_no_retry_with_skip_verify(self, tmp_path, capsys):
        """--skip-verify bypasses the retry path entirely."""
        mod = _load_module()
        argv = _argv(tmp_path, extra=["--skip-verify"])

        commit_attempts = []
        diff_calls = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "commit"]:
                commit_attempts.append(args)
                return _make_completed(1, stderr="something failed")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")  # HEAD never advances
            if args[:3] == ["git", "diff", "--name-only"]:
                diff_calls.append(args)
                return _make_completed(0, stdout="somefile.py\n")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "re-staging reformatted content" not in (captured.out + captured.err)
        # The retry path runs `git diff --name-only` and a second `git commit`.
        # With --skip-verify, neither should fire.
        assert diff_calls == [], (
            "git diff must not be invoked when --skip-verify bypasses the retry path"
        )
        assert len(commit_attempts) == 1, (
            "git commit must only be attempted once when --skip-verify is set"
        )

    def test_no_retry_when_no_reformatted_files(self, tmp_path, capsys):
        """Commit fails but no files were modified by the hook → skip retry, return 3."""
        mod = _load_module()
        argv = _argv(tmp_path)

        commit_attempts = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "commit"]:
                commit_attempts.append(args)
                return _make_completed(1, stderr="some hook error")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")  # HEAD never advances
            if args[:3] == ["git", "diff", "--name-only"]:
                return _make_completed(0, stdout="")  # no reformatted files
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "re-staging reformatted content" not in (captured.out + captured.err)
        # diff returned no reformatted files → no retry of git commit.
        assert len(commit_attempts) == 1, (
            "git commit must only be attempted once when no files were reformatted"
        )

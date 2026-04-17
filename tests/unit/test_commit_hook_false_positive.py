"""Unit tests for tusk-commit.py hook false-positive handling.

Verifies that when `git commit` exits non-zero but the commit actually landed
(HEAD changed), tusk commit exits 0 rather than reporting a fatal failure.
This covers the husky + lint-staged "no staged files" scenario described in
GitHub Issue #329.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    """Load tusk-commit.py as a module without executing __main__."""
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Minimal valid argv as passed by the tusk wrapper: [repo_root, config_path, task_id, message, file]
def _argv(tmp_path, task_id="42", message="my message", files=None):
    config = tmp_path / "config.json"
    config.write_text("{}")
    if files is None:
        # Create the default file so the pre-flight existence check passes
        (tmp_path / "somefile.py").write_text("")
    return [str(tmp_path), str(config), task_id, message] + (files or ["somefile.py"])


class TestHookFalsePositive:
    """git commit exits non-zero but commit lands — should be treated as success."""

    def _make_completed(self, returncode, stdout="", stderr=""):
        r = MagicMock(spec=subprocess.CompletedProcess)
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def test_commit_exits_0_when_head_changes(self, tmp_path):
        """Non-zero git commit exit is forgiven when HEAD advances."""
        mod = _load_module()
        argv = _argv(tmp_path)

        # Map each subprocess.run call to its expected return value in order:
        # 1. tusk lint          → exit 0
        # 2. git add            → exit 0
        # 3. git rev-parse HEAD (pre)  → sha_before
        # 4. git commit         → exit 1 (hook warning)
        # 5. git rev-parse HEAD (post) → sha_after  (HEAD changed!)
        side_effects = [
            self._make_completed(0),                    # lint
            self._make_completed(0),                    # git add
            self._make_completed(0, stdout="aaa111\n"), # pre HEAD
            self._make_completed(1, stderr="lint-staged could not find any staged files."),
            self._make_completed(0, stdout="bbb222\n"), # post HEAD — different!
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0, "Should exit 0 when commit landed despite non-zero hook exit"

    def test_error_printed_when_commit_genuinely_fails(self, tmp_path, capsys):
        """Non-zero git commit AND HEAD unchanged → real failure, exit 3."""
        mod = _load_module()
        argv = _argv(tmp_path)

        side_effects = [
            self._make_completed(0),                    # lint
            self._make_completed(0),                    # git add
            self._make_completed(0, stdout="aaa111\n"), # pre HEAD
            self._make_completed(1, stderr="error: pre-commit hook rejected the commit"),
            self._make_completed(0, stdout="aaa111\n"), # post HEAD — SAME (commit didn't land)
            self._make_completed(0, stdout=""),         # git diff — no reformatted files, skip retry
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, "Should exit 3 when commit genuinely failed"
        captured = capsys.readouterr()
        assert "Error: git commit failed" in captured.err

    def test_no_error_message_on_false_positive(self, tmp_path, capsys):
        """When hook false-positive occurs, 'Error: git commit failed' must not appear."""
        mod = _load_module()
        argv = _argv(tmp_path)

        side_effects = [
            self._make_completed(0),
            self._make_completed(0),
            self._make_completed(0, stdout="aaa111\n"),
            self._make_completed(1, stderr="lint-staged could not find any staged files."),
            self._make_completed(0, stdout="bbb222\n"),
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            mod.main(argv)

        captured = capsys.readouterr()
        assert "Error: git commit failed" not in captured.err
        assert "Error: git commit failed" not in captured.out

    def test_hook_warning_surfaced_as_note(self, tmp_path, capsys):
        """Hook stderr is shown as a 'Note:' (not an error) on false-positive."""
        mod = _load_module()
        argv = _argv(tmp_path)

        hook_warning = "lint-staged could not find any staged files."
        side_effects = [
            self._make_completed(0),
            self._make_completed(0),
            self._make_completed(0, stdout="aaa111\n"),
            self._make_completed(1, stderr=hook_warning),
            self._make_completed(0, stdout="bbb222\n"),
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            mod.main(argv)

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "Note:" in combined
        assert hook_warning in combined

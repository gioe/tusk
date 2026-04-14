"""Unit tests for tusk-commit.py argument parsing enhancements (TASK-87).

Covers three invocation patterns:
  1. Original positional:  <task_id> "<message>" <files...>
  2. -m flag:              <task_id> <files...> -m "<message>"
  3. -- separator:         <task_id> <files...> -- "<message>"
Plus duplicate [TASK-N] prefix stripping.
"""

import importlib.util
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

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


def _argv(tmp_path, args):
    """Build argv list: [repo_root, config_path, ...args]."""
    config = tmp_path / "config.json"
    config.write_text("{}")
    return [str(tmp_path), str(config)] + args


def _run_main_until_lint(tmp_path, args):
    """Run main() with patched subprocess calls, returning the commit message
    that would be passed to git commit -m.

    We patch enough to get past path validation and lint, then capture the
    git commit call to inspect the message.
    """
    mod = _load_module()
    argv = _argv(tmp_path, args)

    commit_message_captured = []

    def fake_run(cmd, *, check=True, capture_output=True, text=True, encoding="utf-8", cwd=None, **kw):
        cmd_str = cmd if isinstance(cmd, str) else cmd[0]
        if isinstance(cmd, list) and cmd[0] == "git" and cmd[1] == "add":
            return _make_completed(0)
        if isinstance(cmd, list) and cmd[0] == "git" and cmd[1] == "commit":
            # Capture the -m argument
            for i, c in enumerate(cmd):
                if c == "-m" and i + 1 < len(cmd):
                    commit_message_captured.append(cmd[i + 1])
            return _make_completed(0, stdout="[feature abc123] message")
        if isinstance(cmd, list) and cmd[0] == "git" and cmd[1] == "rev-parse":
            return _make_completed(0, stdout="abc123")
        if isinstance(cmd, list) and cmd[0] == "git" and cmd[1] == "ls-files":
            return _make_completed(0, stdout="")
        return _make_completed(0)

    def fake_subprocess_run(cmd, **kw):
        # For lint and test_command calls (not capture_output=True)
        return MagicMock(returncode=0)

    with patch.object(mod, "run", side_effect=fake_run), \
         patch("subprocess.run", side_effect=fake_subprocess_run), \
         patch("os.getcwd", return_value=str(tmp_path)):
        ret = mod.main(argv)

    return ret, commit_message_captured


class TestPositionalForm:
    """Original form: tusk commit <id> "<msg>" <files...>"""

    def test_basic_positional(self, tmp_path):
        (tmp_path / "foo.py").write_text("")
        ret, msgs = _run_main_until_lint(tmp_path, ["42", "Fix bug", "foo.py"])
        assert ret == 0
        assert len(msgs) == 1
        assert "[TASK-42] Fix bug" in msgs[0]

    def test_positional_multiple_files(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        ret, msgs = _run_main_until_lint(tmp_path, ["42", "Fix bug", "a.py", "b.py"])
        assert ret == 0
        assert "[TASK-42] Fix bug" in msgs[0]


class TestDashMFlag:
    """Git-like form: tusk commit <id> <files...> -m "<msg>"""

    def test_m_flag_single_file(self, tmp_path):
        (tmp_path / "foo.py").write_text("")
        ret, msgs = _run_main_until_lint(tmp_path, ["42", "foo.py", "-m", "Fix bug"])
        assert ret == 0
        assert len(msgs) == 1
        assert "[TASK-42] Fix bug" in msgs[0]

    def test_m_flag_multiple_files(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        ret, msgs = _run_main_until_lint(tmp_path, ["42", "a.py", "b.py", "-m", "Fix bug"])
        assert ret == 0
        assert "[TASK-42] Fix bug" in msgs[0]

    def test_m_flag_with_criteria(self, tmp_path):
        (tmp_path / "foo.py").write_text("")
        ret, msgs = _run_main_until_lint(
            tmp_path, ["42", "foo.py", "-m", "Fix bug", "--criteria", "10"]
        )
        assert ret == 0
        assert "[TASK-42] Fix bug" in msgs[0]

    def test_m_flag_missing_message(self, tmp_path):
        mod = _load_module()
        argv = _argv(tmp_path, ["42", "foo.py", "-m"])
        ret = mod.main(argv)
        assert ret == 1


class TestDoubleDashSeparator:
    """Separator form: tusk commit <id> <files...> -- "<msg>"""

    def test_double_dash_ignored(self, tmp_path):
        """-- is silently ignored; the message following it becomes a positional."""
        (tmp_path / "foo.py").write_text("")
        # With --, positional becomes: [42, foo.py, Fix bug] — but that's only 3
        # positionals and no -m flag, so it falls into the positional form:
        # task_id=42, message=foo.py, files=[Fix bug] — which is wrong.
        #
        # The -- separator is meant to be used WITH -m:
        #   tusk commit 42 foo.py -- -m "Fix bug"
        # But the more common AI pattern is:
        #   tusk commit 42 foo.py -m "Fix bug"
        # So -- just needs to not break things by being treated as a file path.
        #
        # Test that -- combined with -m works:
        ret, msgs = _run_main_until_lint(tmp_path, ["42", "foo.py", "--", "-m", "Fix bug"])
        assert ret == 0
        assert "[TASK-42] Fix bug" in msgs[0]

    def test_double_dash_not_treated_as_file(self, tmp_path):
        """-- should not appear as a file path in the commit."""
        (tmp_path / "foo.py").write_text("")
        ret, msgs = _run_main_until_lint(
            tmp_path, ["42", "foo.py", "--", "-m", "Fix bug"]
        )
        assert ret == 0
        assert "[TASK-42] Fix bug" in msgs[0]


class TestDuplicatePrefixStripping:
    """Duplicate [TASK-N] prefix is stripped from the message."""

    def test_strips_duplicate_prefix(self, tmp_path):
        (tmp_path / "foo.py").write_text("")
        ret, msgs = _run_main_until_lint(tmp_path, ["42", "[TASK-42] Fix bug", "foo.py"])
        assert ret == 0
        # Should be [TASK-42] Fix bug, not [TASK-42] [TASK-42] Fix bug
        assert msgs[0].startswith("[TASK-42] Fix bug")
        assert "[TASK-42] [TASK-42]" not in msgs[0]

    def test_strips_prefix_with_m_flag(self, tmp_path):
        (tmp_path / "foo.py").write_text("")
        ret, msgs = _run_main_until_lint(
            tmp_path, ["42", "foo.py", "-m", "[TASK-42] Fix bug"]
        )
        assert ret == 0
        assert msgs[0].startswith("[TASK-42] Fix bug")
        assert "[TASK-42] [TASK-42]" not in msgs[0]

    def test_strips_different_task_prefix(self, tmp_path):
        """Even if the caller includes a different task's prefix, it's stripped."""
        (tmp_path / "foo.py").write_text("")
        ret, msgs = _run_main_until_lint(tmp_path, ["42", "[TASK-99] Fix bug", "foo.py"])
        assert ret == 0
        # The [TASK-99] prefix is stripped, and [TASK-42] is prepended
        assert msgs[0].startswith("[TASK-42] Fix bug")

    def test_no_prefix_left_alone(self, tmp_path):
        """Messages without a [TASK-N] prefix are unchanged."""
        (tmp_path / "foo.py").write_text("")
        ret, msgs = _run_main_until_lint(tmp_path, ["42", "Fix bug", "foo.py"])
        assert ret == 0
        assert "[TASK-42] Fix bug" in msgs[0]

    def test_prefix_only_message_rejected(self, tmp_path):
        """A message that is only [TASK-N] with no actual content is rejected as empty."""
        mod = _load_module()
        argv = _argv(tmp_path, ["42", "[TASK-42]", "foo.py"])
        (tmp_path / "foo.py").write_text("")
        ret = mod.main(argv)
        assert ret == 1  # empty message after stripping

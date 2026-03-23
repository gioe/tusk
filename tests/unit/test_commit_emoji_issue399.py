"""Regression test for GitHub Issue #399.

tusk commit should exit 0 when the commit message contains emoji and
git commit + criteria done both succeed.

Root cause: subprocess.run(text=True) without an explicit encoding uses
locale.getpreferredencoding(), which may return 'ASCII' on non-UTF-8
systems.  When git echoes back the commit message (which contains emoji)
in its stdout, Python raises UnicodeDecodeError.  In tusk-criteria.py
run_verification(), this exception is caught by `except Exception` and
returned as {"passed": False}, causing `criteria done` to exit 1 and
`tusk commit` to exit 4 — even though the git commit succeeded.

The fix is to pass encoding="utf-8" explicitly to both subprocess.run
calls so emoji in git output is always decoded correctly.
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


class TestEmojiCommitMessage:
    """tusk commit with emoji in the message must exit 0 when everything succeeds."""

    def test_emoji_in_message_exits_zero(self, tmp_path):
        """Exit 0 when commit message contains emoji and git + criteria done succeed."""
        mod = _load_module()

        target = tmp_path / "fix.py"
        target.write_text("# fixed\n")

        config = tmp_path / "config.json"
        config.write_text("{}")

        # commit message with emoji — the triggering condition from the issue
        argv = [str(tmp_path), str(config), "399", "✅ fix the thing", "fix.py",
                "--criteria", "42"]

        # Call sequence:
        # 1. tusk lint          → exit 0
        # 2. git add            → exit 0
        # 3. git rev-parse HEAD (pre)  → sha_before
        # 4. git commit         → exit 0, stdout echoes emoji commit message
        # 5. tusk criteria done → exit 0
        side_effects = [
            _make_completed(0),                          # lint
            _make_completed(0),                          # git add
            _make_completed(0, stdout="abc111\n"),       # pre HEAD
            _make_completed(0, stdout="[main abc111] [TASK-399] ✅ fix the thing\n 1 file changed"),
            _make_completed(0),                          # tusk criteria done
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0, f"Expected exit 0 with emoji commit message, got {rc}"

    def test_emoji_in_message_no_criteria_exits_zero(self, tmp_path):
        """Exit 0 when commit message contains emoji and no --criteria are passed."""
        mod = _load_module()

        target = tmp_path / "fix.py"
        target.write_text("# fixed\n")

        config = tmp_path / "config.json"
        config.write_text("{}")

        argv = [str(tmp_path), str(config), "399", "⚠️ handle edge case", "fix.py"]

        side_effects = [
            _make_completed(0),                          # lint
            _make_completed(0),                          # git add
            _make_completed(0, stdout="abc222\n"),       # pre HEAD
            _make_completed(0, stdout="[main abc222] [TASK-399] ⚠️ handle edge case\n"),
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0, f"Expected exit 0 with emoji commit message (no criteria), got {rc}"

    def test_run_helper_uses_utf8_encoding(self):
        """run() must specify encoding='utf-8' to prevent locale-dependent failures."""
        mod = _load_module()
        import inspect
        source = inspect.getsource(mod.run)
        assert 'encoding="utf-8"' in source or "encoding='utf-8'" in source, (
            "run() must pass encoding='utf-8' to subprocess.run to handle emoji in git output "
            "on systems where locale.getpreferredencoding() returns a non-UTF-8 encoding"
        )

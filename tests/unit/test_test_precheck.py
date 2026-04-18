"""Unit tests for tusk-test-precheck.py.

Covers the failure modes that the integration tests can't reach because
they require fault injection into the stash-ref-lookup path:

- find_stash_ref_by_message raises (not "") when `git stash list` fails —
  refusing to conflate "command failed" with "no match" is what keeps the
  CLI from reintroducing the silent-data-loss pattern TASK-55 exists to
  prevent.
- When find_stash_ref_by_message returns "" *after* a successful stash
  push, main() must treat it as a hard error (exit 1, recovery message on
  stderr) rather than silently falling through with stashed=False.
"""

import importlib.util
import io
import os
import subprocess
import sys
from unittest import mock

import pytest


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_test_precheck",
    os.path.join(BIN, "tusk-test-precheck.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ---------------------------------------------------------------------------
# find_stash_ref_by_message: must raise on git failure, not return ""
# ---------------------------------------------------------------------------


class TestFindStashRef:
    def test_raises_when_git_stash_list_exits_nonzero(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="fatal: not a git repository\n",
        )
        with mock.patch.object(mod, "_run", return_value=fake):
            with pytest.raises(RuntimeError, match="git stash list failed"):
                mod.find_stash_ref_by_message("/tmp/whatever", "some-message")

    def test_returns_empty_only_when_no_match(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="stash@{0} On main: other-work\n",
            stderr="",
        )
        with mock.patch.object(mod, "_run", return_value=fake):
            ref = mod.find_stash_ref_by_message("/tmp/whatever", "our-message")
        assert ref == ""

    def test_returns_ref_when_match_exists_anywhere_in_stack(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=(
                "stash@{0} On main: intruder\n"
                "stash@{1} On main: our-message-123\n"
                "stash@{2} On main: older\n"
            ),
            stderr="",
        )
        with mock.patch.object(mod, "_run", return_value=fake):
            ref = mod.find_stash_ref_by_message("/tmp/whatever", "our-message-123")
        assert ref == "stash@{1}"


# ---------------------------------------------------------------------------
# main(): silent-data-loss regression — push succeeds, ref lookup returns ""
# ---------------------------------------------------------------------------


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class TestSilentDataLossRegression:
    """When ``git stash push`` succeeds but we cannot locate the named
    stash entry afterwards, the CLI must exit non-zero with a recovery
    message — never silently set ``stashed=False`` and skip the pop."""

    def test_empty_ref_after_successful_push_is_hard_error(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = tmp_path / "config.json"
        cfg.write_text('{"test_command": "true"}')

        # Force the "dirty tree" branch and a successful push, then make
        # find_stash_ref_by_message() return "" — simulating the failure
        # mode under investigation.  The test command itself is never run
        # in this path, and the stash-pop command must never be attempted
        # (we'd be popping a foreign entry).
        monkeypatch.setattr(mod, "detect_dirty", lambda _r: True)
        monkeypatch.setattr(mod, "find_stash_ref_by_message", lambda _r, _m: "")
        monkeypatch.setattr(mod, "run_test", lambda _c, _r: pytest.fail(
            "run_test must not be called when the stash ref is missing — "
            "the CLI must bail out before executing tests"
        ))

        pop_attempted = {"called": False}

        def fake_run(cmd_args, cwd, capture=True):
            if cmd_args[:2] == ["git", "stash"] and cmd_args[2] == "push":
                return _completed(returncode=0)
            if cmd_args[:3] == ["git", "stash", "pop"]:
                pop_attempted["called"] = True
                return _completed(returncode=0)
            return _completed(returncode=0)

        monkeypatch.setattr(mod, "_run", fake_run)

        rc = mod.main([str(repo), str(cfg), "--command", "true"])
        captured = capsys.readouterr()

        assert rc == 1, "must exit non-zero when ref lookup returns empty"
        assert pop_attempted["called"] is False, (
            "must not run `git stash pop` when the named ref cannot be located"
        )
        assert "stash push" in captured.err.lower()
        assert "reported success" in captured.err.lower() or (
            "not in" in captured.err.lower()
        )

    def test_stash_list_failure_after_successful_push_is_hard_error(
        self, tmp_path, monkeypatch, capsys
    ):
        """Same shape as above, but the failure happens because ``git
        stash list`` itself errors — the CLI must still bail out cleanly."""
        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = tmp_path / "config.json"
        cfg.write_text('{"test_command": "true"}')

        def raising_lookup(_r, _m):
            raise RuntimeError("git stash list failed: disk gone")

        monkeypatch.setattr(mod, "detect_dirty", lambda _r: True)
        monkeypatch.setattr(mod, "find_stash_ref_by_message", raising_lookup)
        monkeypatch.setattr(mod, "run_test", lambda _c, _r: pytest.fail(
            "run_test must not be called when stash list itself fails"
        ))

        def fake_run(cmd_args, cwd, capture=True):
            if cmd_args[:2] == ["git", "stash"] and cmd_args[2] == "push":
                return _completed(returncode=0)
            return _completed(returncode=0)

        monkeypatch.setattr(mod, "_run", fake_run)

        rc = mod.main([str(repo), str(cfg), "--command", "true"])
        captured = capsys.readouterr()

        assert rc == 1
        assert "disk gone" in captured.err


# ---------------------------------------------------------------------------
# run_test raising: JSON must not appear, exit must be non-zero
# ---------------------------------------------------------------------------


class TestRunTestRaises:
    def test_run_test_raising_still_triggers_stash_pop_and_returns_nonzero(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = tmp_path / "config.json"
        cfg.write_text('{"test_command": "true"}')

        monkeypatch.setattr(mod, "detect_dirty", lambda _r: True)
        monkeypatch.setattr(
            mod, "find_stash_ref_by_message", lambda _r, _m: "stash@{0}"
        )

        def raising_run_test(_cmd, _cwd):
            raise RuntimeError("simulated subprocess failure")

        monkeypatch.setattr(mod, "run_test", raising_run_test)

        pop_called = {"count": 0}

        def fake_run(cmd_args, cwd, capture=True):
            if cmd_args[:2] == ["git", "stash"] and cmd_args[2] == "push":
                return _completed(returncode=0)
            if cmd_args[:3] == ["git", "stash", "pop"]:
                pop_called["count"] += 1
                return _completed(returncode=0)
            return _completed(returncode=0)

        monkeypatch.setattr(mod, "_run", fake_run)

        rc = mod.main([str(repo), str(cfg), "--command", "true"])
        captured = capsys.readouterr()

        assert rc == 1
        # Cleanup still happened — the whole point of the finally block.
        assert pop_called["count"] == 1
        assert "simulated subprocess failure" in captured.err
        # No JSON payload leaked onto stdout.
        assert captured.out.strip() == ""

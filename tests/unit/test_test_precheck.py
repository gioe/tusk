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
import json
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
# resolve_test_command: domain_test_commands resolution order
# ---------------------------------------------------------------------------


class TestResolveTestCommandDomain:
    """domain_test_commands[domain] sits between path_test_commands and the
    global test_command. Mirrors bin/tusk-commit.py::load_test_command so a
    frontend-only commit doesn't trigger the multi-suite global command on
    the pre-existing-failure check."""

    def _write_cfg(self, tmp_path, payload):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps(payload))
        return str(cfg)

    def test_domain_match_wins_over_global(self, tmp_path):
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {"frontend": "npm test"},
        })
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="frontend",
        )
        assert cmd == "npm test"

    def test_domain_set_but_no_entry_falls_through_to_global(self, tmp_path):
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {"frontend": "npm test"},
        })
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="backend",
        )
        assert cmd == "global"

    def test_no_domain_unchanged_path_then_global(self, tmp_path):
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {"frontend": "npm test"},
        })
        # Default-arg form (no domain kwarg) — proves the existing call
        # sites that never pass `domain` keep their original resolution.
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None,
        )
        assert cmd == "global"

    def test_explicit_command_beats_domain(self, tmp_path):
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {"frontend": "npm test"},
        })
        cmd = mod.resolve_test_command(
            explicit="pytest -x", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="frontend",
        )
        assert cmd == "pytest -x"

    def test_empty_domain_string_skips_lookup(self, tmp_path):
        # Regression guard: argparse default is "" — must behave identically
        # to omitting the kwarg so every existing caller is regression-safe.
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {"": "should-not-match"},
        })
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="",
        )
        assert cmd == "global"


class TestAutoDetectActiveTaskDomain:
    """When --domain is omitted, the resolver consults
    ``_detect_active_task_domain`` to recover the domain of the active task
    from the current branch.  This mirrors what ``tusk commit`` already does
    via ``load_task_domain`` (bin/tusk-commit.py:1098) so both CLIs agree on
    the resolved command for the same in-progress task."""

    def _write_cfg(self, tmp_path, payload):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps(payload))
        return str(cfg)

    def test_auto_detect_picks_domain_command(self, tmp_path, monkeypatch):
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {"scraper": "cd apps/scraper && pytest"},
        })
        monkeypatch.setattr(mod, "_detect_active_task_domain", lambda _r, _s: "scraper")
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="",
        )
        assert cmd == "cd apps/scraper && pytest"

    def test_explicit_domain_overrides_auto_detect(self, tmp_path, monkeypatch):
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {
                "scraper": "scraper-cmd",
                "frontend": "frontend-cmd",
            },
        })
        called = {"count": 0}

        def fake_detect(_r, _s):
            called["count"] += 1
            return "scraper"

        monkeypatch.setattr(mod, "_detect_active_task_domain", fake_detect)
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="frontend",
        )
        assert cmd == "frontend-cmd"
        assert called["count"] == 0, (
            "auto-detect must not run when --domain is passed explicitly"
        )

    def test_auto_detect_skipped_when_domain_test_commands_absent(
        self, tmp_path, monkeypatch
    ):
        cfg = self._write_cfg(tmp_path, {"test_command": "global"})
        called = {"count": 0}

        def fake_detect(_r, _s):
            called["count"] += 1
            return "scraper"

        monkeypatch.setattr(mod, "_detect_active_task_domain", fake_detect)
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="",
        )
        assert cmd == "global"
        assert called["count"] == 0, (
            "auto-detect must not run when no domain_test_commands are configured"
        )

    def test_auto_detect_empty_falls_through_to_global(self, tmp_path, monkeypatch):
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {"scraper": "scraper-cmd"},
        })
        monkeypatch.setattr(mod, "_detect_active_task_domain", lambda _r, _s: "")
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="",
        )
        assert cmd == "global"

    def test_auto_detect_returns_domain_with_no_entry_falls_through(
        self, tmp_path, monkeypatch
    ):
        # Detected domain has no matching entry — resolver must not invent
        # one; it falls through to the global test_command.
        cfg = self._write_cfg(tmp_path, {
            "test_command": "global",
            "domain_test_commands": {"scraper": "scraper-cmd"},
        })
        monkeypatch.setattr(mod, "_detect_active_task_domain", lambda _r, _s: "frontend")
        cmd = mod.resolve_test_command(
            explicit="", config_path=cfg, repo_root=str(tmp_path),
            script_dir="/nonexistent", paths=None, domain="",
        )
        assert cmd == "global"

    def test_detect_helper_returns_empty_without_tusk_binary(self, tmp_path):
        # script_dir points nowhere — the helper short-circuits before any
        # subprocess call.  Guards against tusk_bin not being installed in
        # the precheck environment (e.g. partial installs, sandbox tests).
        assert mod._detect_active_task_domain(str(tmp_path), "/nonexistent/bin") == ""

    def test_detect_helper_returns_empty_when_branch_parse_fails(
        self, tmp_path, monkeypatch
    ):
        # Simulate `tusk branch-parse` exiting non-zero (e.g. on the default
        # branch, where the branch name does not match feature/TASK-<id>-*).
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_bin = bin_dir / "tusk"
        fake_bin.write_text("#!/usr/bin/env bash\nexit 1\n")
        fake_bin.chmod(0o755)
        assert mod._detect_active_task_domain(str(tmp_path), str(bin_dir)) == ""

    def test_detect_helper_returns_empty_on_invalid_branch_parse_json(
        self, tmp_path, monkeypatch
    ):
        # Malformed JSON from branch-parse must not crash the resolver.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_bin = bin_dir / "tusk"
        fake_bin.write_text("#!/usr/bin/env bash\necho 'not json'\nexit 0\n")
        fake_bin.chmod(0o755)
        assert mod._detect_active_task_domain(str(tmp_path), str(bin_dir)) == ""


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


class TestDirtyTreeFallback:
    def test_stash_push_index_failure_runs_tests_in_temporary_worktree(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = tmp_path / "config.json"
        cfg.write_text('{"test_command": "true"}')

        monkeypatch.setattr(mod, "detect_dirty", lambda _r: True)
        monkeypatch.setattr(mod, "run_test", lambda _c, _r: pytest.fail(
            "dirty-tree fallback must not run tests in the original checkout"
        ))

        fallback_calls = []

        def fake_fallback(repo_root, test_command):
            fallback_calls.append((repo_root, test_command))
            return 0

        monkeypatch.setattr(mod, "run_test_in_temporary_worktree", fake_fallback)

        def fake_run(cmd_args, cwd, capture=True):
            if cmd_args[:3] == ["git", "stash", "push"]:
                return _completed(
                    returncode=1,
                    stderr="error: could not write index\n",
                )
            return _completed(returncode=0)

        monkeypatch.setattr(mod, "_run", fake_run)

        rc = mod.main([str(repo), str(cfg), "--command", "true"])
        captured = capsys.readouterr()

        assert rc == 0
        assert fallback_calls == [(str(repo), "true")]
        assert "temporary worktree" in captured.err
        assert json.loads(captured.out) == {
            "pre_existing": False,
            "exit_code": 0,
            "test_command": "true",
            "stashed": False,
        }

    def test_stash_push_index_failure_reports_fallback_setup_error(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = tmp_path / "config.json"
        cfg.write_text('{"test_command": "true"}')

        monkeypatch.setattr(mod, "detect_dirty", lambda _r: True)

        def fake_fallback(_repo_root, _test_command):
            raise RuntimeError("git worktree add fallback failed: locked")

        monkeypatch.setattr(mod, "run_test_in_temporary_worktree", fake_fallback)

        def fake_run(cmd_args, cwd, capture=True):
            if cmd_args[:3] == ["git", "stash", "push"]:
                return _completed(returncode=1, stderr="error: could not write index\n")
            return _completed(returncode=0)

        monkeypatch.setattr(mod, "_run", fake_run)

        rc = mod.main([str(repo), str(cfg), "--command", "true"])
        captured = capsys.readouterr()

        assert rc == 1
        assert "could not write index" in captured.err
        assert "git worktree add fallback failed" in captured.err


class TestGeneratedLockfilePopConflict:
    def test_recreated_scheduled_tasks_lock_is_removed_and_pop_retried(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        lock_path = repo / ".claude" / "scheduled_tasks.lock"
        lock_path.parent.mkdir()
        lock_path.write_text("runtime lock\n")
        cfg = tmp_path / "config.json"
        cfg.write_text('{"test_command": "true"}')

        monkeypatch.setattr(mod, "detect_dirty", lambda _r: True)
        monkeypatch.setattr(
            mod, "find_stash_ref_by_message", lambda _r, _m: "stash@{0}"
        )
        monkeypatch.setattr(mod, "run_test", lambda _c, _r: 0)

        pop_count = {"count": 0}

        def fake_run(cmd_args, cwd, capture=True):
            if cmd_args[:3] == ["git", "stash", "push"]:
                return _completed(returncode=0)
            if cmd_args[:3] == ["git", "stash", "pop"]:
                pop_count["count"] += 1
                if pop_count["count"] == 1:
                    return _completed(
                        returncode=1,
                        stderr=(
                            ".claude/scheduled_tasks.lock already exists, no checkout\n"
                            "error: could not restore untracked files from stash\n"
                        ),
                    )
                return _completed(returncode=0)
            if cmd_args[:3] == ["git", "ls-files", "--error-unmatch"]:
                return _completed(returncode=1)
            return _completed(returncode=0)

        monkeypatch.setattr(mod, "_run", fake_run)

        rc = mod.main([str(repo), str(cfg), "--command", "true"])
        captured = capsys.readouterr()

        assert rc == 0
        assert pop_count["count"] == 2
        assert not lock_path.exists()
        assert "Removed generated file blocking stash restore" in captured.err
        assert json.loads(captured.out)["stashed"] is True

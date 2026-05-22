"""Unit tests for domain-aware test command selection in tusk-commit.py.

Verifies that load_test_command prefers domain_test_commands[domain] over the
global test_command when the task has a matching domain, and falls back correctly
when no domain is set or no matching entry exists.
"""

import importlib.util
import json
import os
import subprocess
from unittest import mock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLoadTestCommand:
    def _write_config(self, tmp_path, data: dict) -> str:
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data))
        return str(p)

    def test_domain_match_returns_domain_command(self, tmp_path):
        """When domain matches a key in domain_test_commands, return that command."""
        mod = _load_module()
        config_path = self._write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"scraper": "cd apps/scraper && python3 -m pytest"},
        })
        assert mod.load_test_command(config_path, "scraper") == "cd apps/scraper && python3 -m pytest"

    def test_no_domain_falls_back_to_global(self, tmp_path):
        """When domain is empty string, return the global test_command."""
        mod = _load_module()
        config_path = self._write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"scraper": "cd apps/scraper && python3 -m pytest"},
        })
        assert mod.load_test_command(config_path, "") == "pytest tests/"

    def test_domain_not_in_domain_test_commands_falls_back_to_global(self, tmp_path):
        """When domain has no entry in domain_test_commands, return global test_command."""
        mod = _load_module()
        config_path = self._write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"scraper": "cd apps/scraper && python3 -m pytest"},
        })
        assert mod.load_test_command(config_path, "cli") == "pytest tests/"

    def test_no_domain_test_commands_key_falls_back_to_global(self, tmp_path):
        """When domain_test_commands is absent from config, return global test_command."""
        mod = _load_module()
        config_path = self._write_config(tmp_path, {"test_command": "pytest tests/"})
        assert mod.load_test_command(config_path, "cli") == "pytest tests/"

    def test_domain_command_empty_string_falls_back_to_global(self, tmp_path):
        """When domain_test_commands[domain] is an empty string, fall back to global."""
        mod = _load_module()
        config_path = self._write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"cli": ""},
        })
        assert mod.load_test_command(config_path, "cli") == "pytest tests/"


class TestLoadTaskDomain:
    """Regression guard for issue #836 — ``load_task_domain`` used to shell out
    via ``tusk shell <SQL>``, a shape that has exited 1 since TASK-287
    forbade positional args to ``tusk shell``.  Silent failure here meant
    ``load_test_command`` always fell through to the global ``test_command``
    no matter how a task's domain was configured."""

    def test_invokes_tusk_with_json_query_not_shell(self):
        mod = _load_module()
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='[{"domain":"cli"}]', stderr="",
        )
        with mock.patch.object(mod.subprocess, "run", return_value=fake) as run:
            assert mod.load_task_domain("/fake/tusk", 42) == "cli"
        call_args = run.call_args.args[0]
        # The first positional arg to subprocess.run is the command list.
        # Critical: must be `tusk -json "<SQL>"`, NOT `tusk shell <SQL>` —
        # the latter has exited 1 since TASK-287 forbade positional SQL args
        # to `tusk shell`, silently zeroing out domain detection.
        assert call_args[1] == "-json", (
            f"load_task_domain must use `tusk -json`, got {call_args!r}"
        )
        assert "shell" not in call_args[:2], (
            f"load_task_domain must NOT use `tusk shell <SQL>` (exits 1 "
            f"since TASK-287), got {call_args!r}"
        )

    def test_returns_domain_string_from_json_payload(self):
        mod = _load_module()
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='[{"domain":"scraper"}]', stderr="",
        )
        with mock.patch.object(mod.subprocess, "run", return_value=fake):
            assert mod.load_task_domain("/fake/tusk", 1) == "scraper"

    def test_empty_domain_string_when_task_has_no_domain(self):
        mod = _load_module()
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='[{"domain":""}]', stderr="",
        )
        with mock.patch.object(mod.subprocess, "run", return_value=fake):
            assert mod.load_task_domain("/fake/tusk", 1) == ""

    def test_empty_string_when_task_id_missing(self):
        mod = _load_module()
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr="",
        )
        with mock.patch.object(mod.subprocess, "run", return_value=fake):
            assert mod.load_task_domain("/fake/tusk", 99999) == ""

    def test_empty_string_on_nonzero_exit(self):
        mod = _load_module()
        fake = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error: db locked",
        )
        with mock.patch.object(mod.subprocess, "run", return_value=fake):
            assert mod.load_task_domain("/fake/tusk", 1) == ""

    def test_empty_string_on_malformed_json(self):
        mod = _load_module()
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json", stderr="",
        )
        with mock.patch.object(mod.subprocess, "run", return_value=fake):
            assert mod.load_task_domain("/fake/tusk", 1) == ""

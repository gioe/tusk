"""Unit tests for direct-invocation guards in tusk-*.py scripts.

Each wrapper-only script should detect when called directly (without the tusk
wrapper passing DB_PATH or REPO_ROOT as argv[1]) and print a clear usage
message instead of crashing with a cryptic error.
"""

import importlib.util
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

# Scripts that expect $DB_PATH (*.db) as argv[1]
DB_PATH_SCRIPTS = [
    ("tusk-autoclose.py", "tusk autoclose"),
    ("tusk-backlog-scan.py", "tusk backlog-scan"),
    ("tusk-check-deliverables.py", "tusk check-deliverables"),
    ("tusk-lint-rules.py", "tusk lint-rule"),
    ("tusk-merge.py", "tusk merge"),
    ("tusk-progress.py", "tusk progress"),
    ("tusk-setup.py", "tusk setup"),
    ("tusk-task-done.py", "tusk task-done"),
    ("tusk-task-get.py", "tusk task-get"),
    ("tusk-task-insert.py", "tusk task-insert"),
    ("tusk-task-list.py", "tusk task-list"),
    ("tusk-task-reopen.py", "tusk task-reopen"),
    ("tusk-task-select.py", "tusk task-select"),
    ("tusk-task-start.py", "tusk task-start"),
    ("tusk-task-update.py", "tusk task-update"),
    ("tusk-test-detect.py", "tusk test-detect"),
]

# Scripts that expect $REPO_ROOT (a directory) as argv[1]
REPO_ROOT_SCRIPTS = [
    ("tusk-branch.py", "tusk branch"),
    ("tusk-commit.py", "tusk commit"),
]


def _run_direct(script_name, extra_args=None):
    """Invoke a script directly (not via the tusk wrapper)."""
    cmd = [sys.executable, os.path.join(BIN, script_name)]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True)


class TestDirectInvocationGuard:
    @pytest.mark.parametrize("script,expected_cmd", DB_PATH_SCRIPTS)
    def test_db_path_script_no_args_exits_nonzero(self, script, expected_cmd):
        result = _run_direct(script)
        assert result.returncode != 0, f"{script} should exit non-zero when called directly"

    @pytest.mark.parametrize("script,expected_cmd", DB_PATH_SCRIPTS)
    def test_db_path_script_no_args_prints_usage(self, script, expected_cmd):
        result = _run_direct(script)
        stderr = result.stderr
        assert "tusk wrapper" in stderr or expected_cmd.split()[1] in stderr, (
            f"{script} should mention the tusk wrapper or correct command in stderr, got: {stderr!r}"
        )

    @pytest.mark.parametrize("script,expected_cmd", DB_PATH_SCRIPTS)
    def test_db_path_script_with_non_db_arg_exits_nonzero(self, script, expected_cmd):
        # Simulates the exact failure from the issue: passing task_id directly
        result = _run_direct(script, ["29", "--reason", "completed"])
        assert result.returncode != 0, (
            f"{script} should exit non-zero when first arg is not a .db path"
        )

    @pytest.mark.parametrize("script,expected_cmd", REPO_ROOT_SCRIPTS)
    def test_repo_root_script_no_args_exits_nonzero(self, script, expected_cmd):
        result = _run_direct(script)
        assert result.returncode != 0, f"{script} should exit non-zero when called directly"

    @pytest.mark.parametrize("script,expected_cmd", REPO_ROOT_SCRIPTS)
    def test_repo_root_script_no_args_prints_usage(self, script, expected_cmd):
        result = _run_direct(script)
        stderr = result.stderr
        assert "tusk wrapper" in stderr or expected_cmd.split()[1] in stderr, (
            f"{script} should mention the tusk wrapper or correct command in stderr, got: {stderr!r}"
        )

    @pytest.mark.parametrize("script,expected_cmd", REPO_ROOT_SCRIPTS)
    def test_repo_root_script_with_non_dir_arg_exits_nonzero(self, script, expected_cmd):
        # Simulates passing a task_id directly instead of REPO_ROOT
        result = _run_direct(script, ["527", "my-slug"])
        assert result.returncode != 0, (
            f"{script} should exit non-zero when first arg is not a directory"
        )

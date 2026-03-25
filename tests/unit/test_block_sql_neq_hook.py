"""Unit tests for the block-sql-neq.sh PreToolUse hook.

Verifies that the hook correctly distinguishes != inside a quoted string argument
(safe, should allow) from != in an unquoted SQL context (should block).

Covers the false-positive scenario from GitHub Issue #411.
"""

import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO_ROOT, ".claude", "hooks", "block-sql-neq.sh")


def _run_hook(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", HOOK],
        input=payload,
        capture_output=True,
        text=True,
    )


class TestBlockSqlNeqHook:
    def test_no_neq_exits_0(self):
        result = _run_hook("tusk task-list")
        assert result.returncode == 0

    def test_neq_in_single_quoted_string_exits_0(self):
        """False-positive scenario from issue #411: != inside single quotes should be allowed."""
        result = _run_hook("tusk conventions add 'In SQL, use <> instead of !='")
        assert result.returncode == 0, (
            "Hook should not fire when != appears inside a single-quoted string argument"
        )

    def test_neq_in_single_quoted_multiword_exits_0(self):
        result = _run_hook("tusk task-insert 'summary' 'description with != operator'")
        assert result.returncode == 0

    def test_neq_unquoted_in_tusk_invocation_exits_2(self):
        """Direct != in a tusk SQL call should still be blocked."""
        result = _run_hook("tusk shell \"SELECT * FROM tasks WHERE priority != 'High'\"")
        assert result.returncode == 2
        assert "Use <>" in result.stdout or "Use <>" in result.stderr

    def test_non_tusk_command_with_neq_exits_0(self):
        """Non-tusk commands with != are not blocked (only tusk SQL is guarded)."""
        result = _run_hook("echo 'value != other'")
        assert result.returncode == 0

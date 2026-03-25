"""Unit tests for the block-sql-neq.sh PreToolUse hook.

Verifies that the hook correctly distinguishes SQL-executing subcommands
(shell, sql-query) from metadata subcommands (commit, task-insert, etc.).

Only SQL-executing subcommands are checked for !=; all others are
unconditionally allowed regardless of quoting.

Covers false-positive scenarios from GitHub Issues #411 (single-quoted args),
#415 (double-quoted commit messages), and #416 (double-quoted SQL is a false
negative with the quote-stripping approach).
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

    def test_neq_in_double_quoted_commit_message_exits_0(self):
        """False-positive from issue #415: != in a double-quoted commit message should be allowed."""
        result = _run_hook('tusk commit 38 "fix false positive on != operator" somefile.py')
        assert result.returncode == 0, (
            "Hook should not fire when != appears inside a double-quoted commit message"
        )

    def test_neq_in_double_quoted_task_summary_exits_0(self):
        """False-positive from issue #415: != in a double-quoted task summary should be allowed."""
        result = _run_hook('tusk task-insert "summary with != operator" "description"')
        assert result.returncode == 0

    def test_neq_unquoted_in_tusk_invocation_exits_2(self):
        """Unquoted != in a tusk context (no surrounding quotes) is still blocked."""
        # Unquoted != — not inside any string literal
        result = _run_hook("tusk shell SELECT * FROM tasks WHERE priority != High")
        assert result.returncode == 2
        assert "Use <>" in result.stdout or "Use <>" in result.stderr

    def test_non_tusk_command_with_neq_exits_0(self):
        """Non-tusk commands with != are not blocked (only tusk SQL is guarded)."""
        result = _run_hook("echo 'value != other'")
        assert result.returncode == 0

    def test_neq_double_quoted_sql_still_blocked(self):
        """False-negative fix from issue #416: != inside a double-quoted SQL arg should be blocked."""
        result = _run_hook('tusk shell "SELECT * FROM tasks WHERE priority != \'High\'"')
        assert result.returncode == 2

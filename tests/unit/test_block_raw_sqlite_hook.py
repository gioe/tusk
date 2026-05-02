"""Unit tests for the block-raw-sqlite.sh PreToolUse hook.

Verifies that the hook blocks real sqlite3 invocations while not firing on
sqlite3 mentions inside quoted string literals (grep regex alternations,
echo/commit messages, etc.).
"""

import json
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO_ROOT, ".claude", "hooks", "block-raw-sqlite.sh")


def _run_hook(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", HOOK],
        input=payload,
        capture_output=True,
        text=True,
    )


class TestBlockRawSqliteHook:
    def test_no_sqlite3_exits_0(self):
        result = _run_hook("tusk task-list")
        assert result.returncode == 0

    def test_bare_sqlite3_invocation_blocked(self):
        result = _run_hook("sqlite3 tusk/tasks.db 'SELECT 1'")
        assert result.returncode == 2

    def test_sqlite3_after_pipe_blocked(self):
        result = _run_hook("cat query.sql | sqlite3 tusk/tasks.db")
        assert result.returncode == 2

    def test_sqlite3_after_and_operator_blocked(self):
        result = _run_hook("echo hi && sqlite3 tusk/tasks.db")
        assert result.returncode == 2

    def test_sqlite3_after_or_operator_blocked(self):
        result = _run_hook("test -f x || sqlite3 tusk/tasks.db")
        assert result.returncode == 2

    def test_sqlite3_after_semicolon_blocked(self):
        result = _run_hook("echo hi; sqlite3 tusk/tasks.db")
        assert result.returncode == 2

    def test_sqlite3_in_command_substitution_blocked(self):
        result = _run_hook("x=$(sqlite3 tusk/tasks.db 'SELECT 1')")
        assert result.returncode == 2

    def test_grep_alternation_with_sqlite3_not_blocked(self):
        """False-positive fix: | inside a quoted regex is not a shell pipe."""
        result = _run_hook('grep -E "foo|sqlite3" file.txt')
        assert result.returncode == 0, (
            "Hook should not fire when sqlite3 appears inside a quoted regex alternation"
        )

    def test_echo_double_quoted_mentioning_sqlite3_not_blocked(self):
        result = _run_hook('echo "direct sqlite3 is blocked"')
        assert result.returncode == 0

    def test_git_commit_message_mentioning_sqlite3_not_blocked(self):
        result = _run_hook('git commit -m "fix sqlite3 regression" file.py')
        assert result.returncode == 0

    def test_single_quoted_alternation_with_sqlite3_not_blocked(self):
        result = _run_hook("grep -E 'foo|sqlite3' file.txt")
        assert result.returncode == 0

    def test_single_quoted_echo_with_sqlite3_not_blocked(self):
        result = _run_hook("echo 'direct sqlite3 is blocked'")
        assert result.returncode == 0

    def test_semicolon_inside_quoted_string_not_blocked(self):
        result = _run_hook('echo "x;sqlite3 y"')
        assert result.returncode == 0

    def test_real_sqlite3_after_quoted_string_still_blocked(self):
        """Stripping quotes must not hide a real invocation that follows."""
        result = _run_hook('echo "harmless" && sqlite3 tusk/tasks.db')
        assert result.returncode == 2

    def test_heredoc_unquoted_body_with_sqlite3_not_blocked(self):
        """Issue #638: forbidden tokens inside <<EOF heredoc bodies are data."""
        cmd = "X=$(cat <<EOF\nsqlite3 foo.db SELECT 1\nEOF\n)"
        result = _run_hook(cmd)
        assert result.returncode == 0, (
            "Hook should not fire on sqlite3 inside an unquoted heredoc body"
        )

    def test_heredoc_single_quoted_opener_with_sqlite3_not_blocked(self):
        cmd = "cat <<'EOF'\nsqlite3 foo.db SELECT 1\nEOF"
        result = _run_hook(cmd)
        assert result.returncode == 0

    def test_heredoc_double_quoted_opener_with_sqlite3_not_blocked(self):
        cmd = 'cat <<"EOF"\nsqlite3 foo.db SELECT 1\nEOF'
        result = _run_hook(cmd)
        assert result.returncode == 0

    def test_heredoc_custom_token_with_sqlite3_not_blocked(self):
        """Closing token must match the user-chosen identifier, not just EOF."""
        cmd = "cat <<MYDATA\nsqlite3 foo.db SELECT 1\nMYDATA"
        result = _run_hook(cmd)
        assert result.returncode == 0

    def test_heredoc_dash_variant_with_sqlite3_not_blocked(self):
        """`<<-TOKEN` allows leading tab on body and closer; still data."""
        cmd = "cat <<-EOF\n\tsqlite3 foo.db SELECT 1\n\tEOF"
        result = _run_hook(cmd)
        assert result.returncode == 0

    def test_real_sqlite3_after_heredoc_still_blocked(self):
        """A real invocation following a heredoc must still be caught."""
        cmd = "cat <<EOF\nsome data\nEOF\nsqlite3 foo.db SELECT 1"
        result = _run_hook(cmd)
        assert result.returncode == 2

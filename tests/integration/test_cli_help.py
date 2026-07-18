"""End-to-end coverage for command help routing through bin/tusk."""

import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_PATH = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [TUSK_PATH, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        timeout=10,
    )


@pytest.mark.parametrize(
    "command",
    ["branch", "commit", "check-deliverables", "merge", "abandon", "progress"],
)
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_manual_task_id_commands_print_help_before_validation(command, flag):
    result = _run(command, flag)

    assert result.returncode == 0
    assert result.stdout.startswith(f"Usage: tusk {command} ")
    assert "Invalid task ID" not in result.stderr


@pytest.mark.parametrize("command", ["progress", "merge"])
def test_help_command_matches_direct_command_help(command):
    direct = _run(command, "--help")
    uniform = _run("help", command)

    assert uniform.returncode == 0
    assert uniform.stdout == direct.stdout
    assert uniform.stderr == direct.stderr


def test_help_command_delegates_to_argparse_backed_command():
    result = _run("help", "task-get")

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert "task_id" in result.stdout


def test_help_guard_prevents_session_recalc_work():
    result = _run("session-recalc", "--help")

    assert result.returncode == 0
    assert result.stdout == "Usage: tusk session-recalc\n"
    assert "Found " not in result.stdout
    assert result.stderr == ""


def test_help_unknown_command_preserves_unknown_subcommand_error():
    result = _run("help", "not-a-tusk-command")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "Unknown subcommand 'not-a-tusk-command'" in result.stderr

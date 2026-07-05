import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run_help(db_path, *args):
    return subprocess.run(
        [TUSK_BIN, *args],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )


def test_dash_dash_help_prints_tusk_usage_and_exits_zero(db_path):
    result = _run_help(db_path, "--help")

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "Usage: tusk" in combined
    assert "task-list" in combined


def test_help_prints_tusk_usage_and_exits_zero(db_path):
    result = _run_help(db_path, "help")

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "Usage: tusk" in combined
    assert "task-list" in combined


def test_dash_dash_help_does_not_print_sqlite_usage(db_path):
    result = _run_help(db_path, "--help")

    combined = result.stdout + result.stderr
    assert "Usage: sqlite3" not in combined

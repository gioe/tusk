"""Regression for issue #591: `bin/tusk shell` silently swallowed positional SQL.

Before the fix, `cmd_shell` was implemented as a bare
``exec sqlite3 -header -column -cmd "PRAGMA foreign_keys = ON;" "$DB_PATH"``
and the dispatcher arm (``shell)  cmd_shell ;;``) did not forward ``"$@"``.
Calling ``tusk shell "SELECT 1"`` therefore discarded the SQL entirely:
sqlite3 entered interactive mode, read EOF on stdin, and exited 0 with no
output. The command appeared to succeed and produced nothing — a silent
failure that violates the Transparent pillar.

The fix refuses positional args with a clear error pointing users at the
``tusk -column -header "<SQL>"`` form (cmd_query) for one-off queries.
"""

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def test_shell_with_positional_sql_exits_nonzero(db_path):
    result = subprocess.run(
        [TUSK_BIN, "shell", "SELECT 1 AS x"],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    assert result.returncode != 0, (
        "tusk shell with positional args must exit non-zero; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_shell_with_positional_sql_points_at_cmd_query(db_path):
    result = subprocess.run(
        [TUSK_BIN, "shell", "SELECT 1 AS x"],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    combined = result.stdout + result.stderr
    assert "tusk -column -header" in combined, (
        "error message should point users at the cmd_query form for one-off queries; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_shell_with_positional_sql_emits_to_stderr(db_path):
    result = subprocess.run(
        [TUSK_BIN, "shell", "SELECT 1 AS x"],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    assert result.stdout == "", (
        f"usage error must go to stderr, not stdout; stdout={result.stdout!r}"
    )
    assert "tusk shell" in result.stderr


def test_shell_with_no_args_runs_sqlite3(db_path):
    """Bare ``tusk shell`` must still launch sqlite3 — the new guard fires
    only when positional args are supplied. With stdin closed, sqlite3
    reads EOF immediately and exits 0 without entering the REPL loop, so
    the call is non-interactive-safe under capture."""
    result = subprocess.run(
        [TUSK_BIN, "shell"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    assert result.returncode == 0, (
        "bare `tusk shell` with closed stdin should exit 0 (sqlite3 reads EOF and exits); "
        f"got code={result.returncode} stderr={result.stderr!r}"
    )

"""Regression tests for the silent exit-14 preflight path (issue #1078).

``preflight_schema_version`` reads ``PRAGMA user_version`` via the sqlite3
CLI. The CLI propagates SQLite error codes at statement-step time (e.g. 14 =
SQLITE_CANTOPEN when a tasks.db-wal / tasks.db-shm sidecar is corrupted or
permission-blocked). The previous form — an unguarded assignment with
``2>/dev/null`` under ``set -euo pipefail`` — killed bin/tusk with sqlite3's
bare exit code and zero output, which looked like an internal tusk crash.
The fix captures the exit code and stderr and prints an actionable
diagnostic naming the cwd, binary, and db path.
"""

import os
import shutil
import sqlite3
import subprocess
import textwrap

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _make_wal_db(directory):
    """A minimal WAL-mode DB whose sidecars have been removed."""
    db = os.path.join(str(directory), "tasks.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    for ext in ("-wal", "-shm"):
        try:
            os.remove(db + ext)
        except FileNotFoundError:
            pass
    return db


def _run_tusk(args, db, cwd, binary=TUSK_BIN):
    env = os.environ.copy()
    env["TUSK_DB"] = db
    env["TUSK_QUIET"] = "1"
    env.pop("TUSK_GUARD_ACTIVE", None)
    return subprocess.run(
        [binary, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _run_tusk_with_env(args, db, cwd, env_overrides):
    env = os.environ.copy()
    env["TUSK_DB"] = db
    env["TUSK_QUIET"] = "1"
    env.pop("TUSK_GUARD_ACTIVE", None)
    env.update(env_overrides)
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_corrupted_wal_sidecar_prints_actionable_diagnostic(tmp_path):
    """The incident shape: sqlite3 exits 14 (SQLITE_CANTOPEN) because the
    -wal sidecar is unusable. tusk must not die silently with exit 14 —
    it prints a diagnostic naming the db path, cwd, and likely causes."""
    db = _make_wal_db(tmp_path)
    os.makedirs(db + "-wal")  # a directory where the WAL file should be

    result = _run_tusk(["task-list"], db=db, cwd=tmp_path)

    assert result.returncode == 1, (
        f"expected diagnostic exit 1, got {result.returncode};\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "cannot read the schema version" in result.stderr
    assert db in result.stderr
    assert "cwd:" in result.stderr
    assert "tasks.db-wal" in result.stderr
    assert result.stderr.strip(), "stderr must never be empty on this path"


def test_relative_binary_from_subdirectory_still_diagnoses(tmp_path):
    """Mirror the issue #1078 invocation shape: a relative binary path from
    a subdirectory. The diagnostic (not a bare silent exit) must appear."""
    db = _make_wal_db(tmp_path)
    os.makedirs(db + "-wal")
    subdir = tmp_path / "apps" / "scraper"
    subdir.mkdir(parents=True)
    rel_binary = os.path.relpath(TUSK_BIN, str(subdir))

    result = _run_tusk(
        ["task-start", "1", "--force"], db=db, cwd=subdir, binary=rel_binary
    )

    assert result.returncode == 1, (
        f"expected diagnostic exit 1, got {result.returncode};\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "cannot read the schema version" in result.stderr
    assert "db path:" in result.stderr


def test_healthy_db_does_not_trigger_diagnostic(tmp_path):
    """Control: with readable sidecar state the preflight passes and the
    new diagnostic never fires (downstream may still fail on missing
    tables — that is a different, louder error)."""
    db = _make_wal_db(tmp_path)

    result = _run_tusk(["task-list"], db=db, cwd=tmp_path)

    assert "cannot read the schema version" not in result.stderr


def test_transient_schema_lock_is_retried(tmp_path):
    """A transient SQLITE_BUSY from the shell preflight should be retried
    before the user-facing unopenable-db diagnostic fires."""
    db = _make_wal_db(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_file = tmp_path / "sqlite-attempts"
    fake_sqlite = fake_bin / "sqlite3"
    real_sqlite = shutil.which("sqlite3")
    assert real_sqlite is not None
    fake_sqlite.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "${{2:-}}" == "PRAGMA user_version" && ! -f "{state_file}" ]]; then
              touch "{state_file}"
              echo "Error: database is locked" >&2
              exit 5
            fi
            exec "{real_sqlite}" "$@"
            """
        ),
        encoding="utf-8",
    )
    fake_sqlite.chmod(0o755)

    result = _run_tusk_with_env(
        ["task-list"],
        db=db,
        cwd=tmp_path,
        env_overrides={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
    )

    assert "cannot read the schema version" not in result.stderr
    assert state_file.exists(), "fake sqlite3 must have exercised the retry path"

"""Integration test for the schema-version preflight in bin/tusk.

When the DB's PRAGMA user_version exceeds the highest registered migration in
the binary's MIGRATIONS list, normal subcommands must exit non-zero with an
actionable upgrade message — not a raw sqlite3.OperationalError. This covers
the version-skew case from issue #636 (motivated by closed #624: a v780
binary on a v782+ DB hit 'no such column: is_deferred' instead of an
upgrade prompt).
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate", os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


def _supported_schema_max():
    return max(v for v, _ in tusk_migrate.MIGRATIONS)


def _stamp_user_version(db, version):
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


def _run(args, db_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    return subprocess.run(
        [TUSK_BIN, *args], capture_output=True, text=True, env=env,
    )


def test_normal_subcmd_blocks_when_db_ahead(db_path):
    """tusk task-list against a DB stamped past SUPPORTED_SCHEMA_MAX exits
    non-zero with the actionable mismatch message — not a raw sqlite error."""
    supported = _supported_schema_max()
    _stamp_user_version(db_path, supported + 1)

    result = _run(["task-list"], db_path)

    assert result.returncode != 0
    assert "Schema mismatch" in result.stderr
    assert f"v{supported + 1}" in result.stderr
    assert f"<=v{supported}" in result.stderr
    assert "tusk upgrade" in result.stderr
    # Crucially, the raw sqlite failure mode this preflight replaces must
    # NOT surface to the user.
    assert "OperationalError" not in result.stderr
    assert "no such column" not in result.stderr


def test_at_supported_max_does_not_fire(db_path):
    """user_version == SUPPORTED_SCHEMA_MAX is the happy path: preflight stays
    silent and the subcommand runs normally."""
    _stamp_user_version(db_path, _supported_schema_max())

    result = _run(["task-list"], db_path)

    assert result.returncode == 0
    assert "Schema mismatch" not in result.stderr


@pytest.mark.parametrize("argv", [
    ["path"],
    ["version"],
    ["migrate"],
    ["init", "--force", "--skip-gitignore"],
    ["regen-triggers"],
])
def test_recovery_subcmds_bypass_preflight(db_path, argv):
    """Recovery + DB-less subcommands must remain runnable when the DB is
    ahead of the binary — otherwise the very tools that fix the mismatch
    would themselves be locked out (criteria #3)."""
    _stamp_user_version(db_path, _supported_schema_max() + 1)

    result = _run(argv, db_path)

    assert "Schema mismatch" not in result.stderr, (
        f"Preflight fired for recovery subcommand {argv!r}:\n{result.stderr}"
    )


def test_reverse_direction_unaffected(db_path):
    """Binary-newer-than-DB stays tusk migrate's job (criteria #4). Stamping
    user_version BELOW supported_max must not trip the forward preflight."""
    supported = _supported_schema_max()
    # Stamp 1 below — far enough to be unambiguously 'older'. Real older DBs
    # may also be missing columns, but that is migrate's domain, not ours.
    _stamp_user_version(db_path, max(supported - 1, 1))

    result = _run(["task-list"], db_path)

    assert "Schema mismatch" not in result.stderr
    # Preflight didn't fire — exit code is whatever task-list itself returned.

"""Shared pytest fixtures for tusk tests.

All Python scripts under bin/ accept:
    sys.argv[1] — db_path
    sys.argv[2] — config_path
    sys.argv[3:] — command-specific flags

Use these fixtures as the foundation for all unit and integration tests.
"""

import os
import subprocess

import pytest

# Repo root is the parent of the tests/ directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

# SQLite note: DB trigger and CHECK violations raise sqlite3.IntegrityError,
# NOT sqlite3.OperationalError. Use IntegrityError in pytest.raises() for trigger tests.


@pytest.fixture()
def config_path():
    """Return the path to config.default.json."""
    return os.path.join(REPO_ROOT, "config.default.json")


@pytest.fixture()
def db_path(tmp_path, config_path, monkeypatch):
    """Initialise a fresh tusk SQLite DB in tmp_path via `bin/tusk init`.

    Pins TUSK_DB in the test's environment via monkeypatch so that *any*
    subprocess a test (or its code-under-test) spawns inherits the override
    and hits this isolated DB. Without this, helpers like tusk-abandon and
    tusk-merge — which shell out to `tusk session-close` / `tusk task-done`
    — silently hit the repo's live tusk/tasks.db and produce opaque
    "Session N already closed" / "Task N already Done" failures that
    reference IDs from the live DB. See TASK-53.

    Path-layout gotcha: the DB sits at ``tmp_path/tasks.db`` (flat), not at
    ``tmp_path/tusk/tasks.db`` (production layout). Any code-under-test that
    resolves a repo-rooted path via ``dirname(dirname(db_path))/<subpath>``
    (e.g. migration 29 looking for ``tusk/conventions.md``, migration 47
    looking for ``docs/PILLARS.md``) will land at the *parent* of
    ``tmp_path``, not inside it. For those tests, build a nested fixture —
    create ``tmp_path/tusk/tasks.db`` with ``TUSK_DB`` pinned to that path
    and place the sibling files under ``tmp_path/...`` — so that
    ``dirname(dirname(db))`` resolves to ``tmp_path``. See
    ``tests/integration/test_migrate_47.py::repo_with_pillars_md`` for the
    canonical pattern.
    """
    db_file = tmp_path / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"tusk init failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert db_file.exists(), f"Expected DB at {db_file} after tusk init"
    return db_file

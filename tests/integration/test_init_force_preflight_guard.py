"""Regression test for issue #533: `tusk init --force` must not wipe the
live database when a preflight step (config copy, gitignore/CLAUDE.md
update, or `validate_config`) fails between the old `rm "$DB_PATH"` and
the SCHEMA block.

Before the fix, `cmd_init` ordered operations as:
    1. cp DB → .bak.<ts>
    2. rm DB                      ← destructive
    3. copy default config
    4. update gitignore + CLAUDE.md
    5. validate_config            ← exits non-zero on damaged config
    6. SCHEMA sqlite3 block       ← never reached

With `set -euo pipefail`, step 5's non-zero exit terminated the script
after step 2 had already removed the live DB, and the `.bak.<ts>` was
never restored automatically. The fix reorders so the destructive `rm`
runs only after every preflight step has passed.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


@pytest.fixture()
def git_tmp(tmp_path):
    """A tmp_path with a bare git repo so find_repo_root resolves to it."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    return tmp_path


def _init(tmp_path, *, extra_env=None):
    db_file = tmp_path / "tusk" / "tasks.db"
    env = {**os.environ, "TUSK_DB": str(db_file)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore", "--yes"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
    ), db_file


def test_force_reinit_preserves_db_when_validate_config_fails(git_tmp):
    """Damaged config.json makes validate_config exit non-zero. The live DB
    must survive — not be pre-wiped by a destructive step that ran before
    preflight."""
    first, db_file = _init(git_tmp)
    assert first.returncode == 0, (
        f"fresh init should succeed:\nSTDOUT:{first.stdout}\nSTDERR:{first.stderr}"
    )
    assert db_file.exists()
    original_size = db_file.stat().st_size
    assert original_size > 0, "fresh init should produce a non-empty DB"

    # Damage the project config so validate_config rejects it.
    config_path = git_tmp / "tusk" / "config.json"
    config_path.write_text("not valid json")

    second, _ = _init(git_tmp, extra_env={"TUSK_QUIET": "1"})

    # Preflight failure must exit non-zero.
    assert second.returncode != 0, (
        "init should fail when config.json is malformed; "
        f"stdout={second.stdout!r} stderr={second.stderr!r}"
    )

    # Regression guard for issue #533: the DB must not have been pre-wiped.
    assert db_file.exists(), (
        "tusk/tasks.db was deleted despite validate_config failing — "
        "the destructive rm ran before preflight"
    )
    assert db_file.stat().st_size > 0, (
        f"tusk/tasks.db was truncated to 0 bytes (size={db_file.stat().st_size}) — "
        "destructive operation ran before preflight completed"
    )


def test_force_reinit_happy_path_still_recreates_db(git_tmp):
    """Sanity guard: the reordering must not break the normal --force flow.
    A valid config should still produce a freshly recreated DB."""
    first, db_file = _init(git_tmp)
    assert first.returncode == 0
    assert db_file.exists() and db_file.stat().st_size > 0

    # Rerun with --force on a valid config — should succeed and leave a valid DB.
    second, _ = _init(git_tmp)
    assert second.returncode == 0, (
        f"happy-path --force should still succeed:\nSTDOUT:{second.stdout}\nSTDERR:{second.stderr}"
    )
    assert db_file.exists() and db_file.stat().st_size > 0

    # Verify the schema is intact by opening the DB and reading a core table.
    import sqlite3
    conn = sqlite3.connect(str(db_file))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchall()
        assert rows == [("tasks",)], "tasks table missing after --force recreate"
    finally:
        conn.close()

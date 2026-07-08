import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
MIGRATE_PATH = os.path.join(BIN, "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate_86", MIGRATE_PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _columns(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _version(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_migrate_86_adds_skill_run_transcript_path(tmp_path):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE skill_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL
        );
        PRAGMA user_version = 85;
        """
    )
    conn.close()

    mod.migrate_86(str(db_path), "", BIN)

    assert _version(db_path) == 86
    assert "transcript_path" in _columns(db_path, "skill_runs")


def test_migrate_86_is_idempotent_when_already_at_v86(tmp_path):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE skill_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            transcript_path TEXT
        );
        PRAGMA user_version = 86;
        """
    )
    conn.close()

    mod.migrate_86(str(db_path), "", BIN)

    assert _version(db_path) == 86
    assert "transcript_path" in _columns(db_path, "skill_runs")

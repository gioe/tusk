import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
MIGRATE_PATH = os.path.join(BIN, "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate_87", MIGRATE_PATH)
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


def test_migrate_87_adds_provider_aware_telemetry_columns(tmp_path):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE task_sessions (id INTEGER PRIMARY KEY);
        CREATE TABLE skill_runs (id INTEGER PRIMARY KEY, transcript_path TEXT);
        PRAGMA user_version = 86;
        """
    )
    conn.close()

    mod.migrate_87(str(db_path), "", BIN)

    assert _version(db_path) == 87
    assert {"transcript_path", "transcript_provider", "telemetry_status"} <= _columns(
        db_path, "task_sessions"
    )
    assert {"transcript_provider", "telemetry_status"} <= _columns(db_path, "skill_runs")


def test_migrate_87_is_idempotent_when_already_at_v87(tmp_path):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE task_sessions (
            id INTEGER PRIMARY KEY,
            transcript_path TEXT,
            transcript_provider TEXT,
            telemetry_status TEXT
        );
        CREATE TABLE skill_runs (
            id INTEGER PRIMARY KEY,
            transcript_provider TEXT,
            telemetry_status TEXT
        );
        PRAGMA user_version = 87;
        """
    )
    conn.close()

    mod.migrate_87(str(db_path), "", BIN)

    assert _version(db_path) == 87

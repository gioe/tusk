"""Integration test for migration 80: null blank verification_spec rows (issue #1045)."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)


def _create_v79_shape(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS acceptance_criteria;

            CREATE TABLE acceptance_criteria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                criterion TEXT NOT NULL,
                source TEXT DEFAULT 'original',
                is_completed INTEGER DEFAULT 0,
                is_deferred INTEGER DEFAULT 0,
                deferred_reason TEXT,
                criterion_type TEXT DEFAULT 'manual',
                verification_spec TEXT
            );

            INSERT INTO acceptance_criteria (task_id, criterion, criterion_type, verification_spec)
            VALUES
                (1, 'empty spec', 'manual', ''),
                (1, 'spaces spec', 'manual', '   '),
                (1, 'tab-newline spec', 'manual', char(9) || char(10)),
                (2, 'real spec', 'test', 'pytest -q'),
                (2, 'null spec', 'manual', NULL);

            PRAGMA user_version = 79;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _specs_by_criterion(db_path) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT criterion, verification_spec FROM acceptance_criteria"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


def test_migrate_80_nulls_blank_specs(db_path, config_path):
    _create_v79_shape(db_path)

    tusk_migrate.migrate_80(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    specs = _specs_by_criterion(db_path)
    assert specs["empty spec"] is None
    assert specs["spaces spec"] is None
    assert specs["tab-newline spec"] is None
    assert specs["real spec"] == "pytest -q"
    assert specs["null spec"] is None

    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == 80


def test_fresh_init_at_or_past_v80(db_path):
    # cmd_init stamps the latest schema version so fresh installs never need
    # this migration.
    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version >= 80


def test_idempotent_when_already_at_v80(db_path, config_path):
    # Fresh DBs initialize at the latest schema; stamp 80 explicitly so this
    # test keeps passing when later migrations land (see CLAUDE.md checklist).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 80")
        conn.execute(
            "INSERT INTO tasks (summary, status) VALUES ('m80 idempotent host', 'To Do')"
        )
        task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, criterion_type, verification_spec)"
            " VALUES (?, 'kept spec', 'test', 'pytest -q')",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    tusk_migrate.migrate_80(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    specs = _specs_by_criterion(db_path)
    assert specs["kept spec"] == "pytest -q"

    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version >= 80

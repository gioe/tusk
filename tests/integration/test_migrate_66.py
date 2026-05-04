"""Integration test for migrate_66: add resolution_note column to review_comments.

Covers:
- schema version advances 65 → 66
- review_comments.resolution_note column is added (nullable TEXT)
- pre-existing rows survive with resolution_note IS NULL
- new resolutions can persist non-null notes alongside resolution
- idempotent short-circuit on re-run against a fresh v66 install
"""

import importlib.util
import os
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate",
        os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


@pytest.fixture()
def db_at_v65_with_existing_resolutions(db_path, config_path):
    """Reconstitute a DB shaped like v65: review_comments without resolution_note.
    Stamp PRAGMA user_version=65. Seed both open and resolved rows so the
    migration's effect on existing data can be observed.
    """
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        DROP TABLE IF EXISTS review_comments;
        CREATE TABLE review_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            file_path TEXT,
            line_start INTEGER,
            line_end INTEGER,
            category TEXT,
            severity TEXT,
            comment TEXT NOT NULL,
            resolution TEXT DEFAULT NULL
                CHECK (resolution IN ('fixed', 'dismissed')),
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (review_id) REFERENCES code_reviews(id) ON DELETE CASCADE
        );
        CREATE INDEX idx_review_comments_review_id ON review_comments(review_id);

        INSERT INTO code_reviews (id, task_id, status) VALUES (1, 1, 'pending');
        INSERT INTO tasks (id, summary, status) VALUES (1, 'Host', 'In Progress');

        INSERT INTO review_comments
            (id, review_id, category, severity, comment, resolution)
        VALUES
            (10, 1, 'must_fix', 'major', 'open finding',  NULL),
            (11, 1, 'must_fix', 'major', 'fix landed',    'fixed'),
            (12, 1, 'suggest',  'minor', 'legacy dismiss','dismissed');

        PRAGMA user_version = 65;
        """
    )
    conn.commit()
    conn.close()
    return db


def test_migrate_66_adds_resolution_note_column(
    db_at_v65_with_existing_resolutions, config_path
):
    db = db_at_v65_with_existing_resolutions
    assert tusk_migrate.get_version(db) == 65

    tusk_migrate.migrate_66(db, config_path, SCRIPT_DIR)

    assert tusk_migrate.get_version(db) == 66

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_comments)").fetchall()}
    assert "resolution_note" in cols, (
        "resolution_note must be added by migrate_66"
    )

    rows = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, resolution, resolution_note FROM review_comments ORDER BY id"
    ).fetchall()}
    assert rows[10]["resolution_note"] is None
    assert rows[11]["resolution_note"] is None
    assert rows[12]["resolution_note"] is None

    conn.execute(
        "UPDATE review_comments SET resolution = ?, resolution_note = ? WHERE id = ?",
        ("dismissed", "Tracked as TASK-99", 10),
    )
    conn.commit()
    note = conn.execute(
        "SELECT resolution_note FROM review_comments WHERE id = 10"
    ).fetchone()["resolution_note"]
    assert note == "Tracked as TASK-99"

    conn.close()


def test_migrate_66_idempotent_when_already_at_v66(db_path, config_path):
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 66")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_66(db, config_path, SCRIPT_DIR)
    assert tusk_migrate.get_version(db) >= 66

"""Integration test for migrate_65: drop the deferral mechanism from review_comments.

Covers:
- schema version advances 64 → 65
- review_comments.deferred_task_id column is removed
- resolution CHECK constraint narrowed to ('fixed', 'dismissed')
- pre-existing rows survive: category='defer' becomes 'suggest';
  resolution='deferred' becomes 'dismissed'; non-deferral rows untouched
- idempotent short-circuit on re-run against a fresh v65 install
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
def db_at_v64_with_deferral(db_path, config_path):
    """Reconstitute a DB shaped like v64: review_comments has deferred_task_id
    and resolution CHECK includes 'deferred'. Stamp PRAGMA user_version=64.

    Fresh installs ship v65+, so the column and the wider CHECK are absent.
    Recreate the v64 shape and seed mixed-state rows.
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
                CHECK (resolution IN ('fixed', 'deferred', 'dismissed')),
            deferred_task_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_review_comments_review_id ON review_comments(review_id);

        INSERT INTO code_reviews (id, task_id, status) VALUES (1, 1, 'pending');
        INSERT INTO tasks (id, summary, status) VALUES
            (1, 'Host task', 'In Progress'),
            (101, 'Deferred follow-up', 'To Do');

        INSERT INTO review_comments
            (id, review_id, category, severity, comment, resolution, deferred_task_id)
        VALUES
            (10, 1, 'defer',    'minor', 'defer A',   'deferred',  101),
            (11, 1, 'defer',    'minor', 'defer B',   'deferred',  NULL),
            (12, 1, 'defer',    'minor', 'defer C',   NULL,        NULL),
            (13, 1, 'must_fix', 'major', 'must_fix',  'fixed',     NULL),
            (14, 1, 'suggest',  'minor', 'suggest',   'dismissed', NULL);

        PRAGMA user_version = 64;
        """
    )
    conn.commit()
    conn.close()
    return db


def test_migrate_65_drops_column_and_remaps_data(db_at_v64_with_deferral, config_path):
    db = db_at_v64_with_deferral
    assert tusk_migrate.get_version(db) == 64

    tusk_migrate.migrate_65(db, config_path, SCRIPT_DIR)

    assert tusk_migrate.get_version(db) == 65

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_comments)").fetchall()}
    assert "deferred_task_id" not in cols, (
        "deferred_task_id must be dropped after migrate_65"
    )

    rows = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, category, resolution, comment FROM review_comments ORDER BY id"
    ).fetchall()}

    assert rows[10]["category"] == "suggest"
    assert rows[10]["resolution"] == "dismissed"
    assert rows[11]["category"] == "suggest"
    assert rows[11]["resolution"] == "dismissed"
    assert rows[12]["category"] == "suggest"
    assert rows[12]["resolution"] is None
    assert rows[13]["category"] == "must_fix"
    assert rows[13]["resolution"] == "fixed"
    assert rows[14]["category"] == "suggest"
    assert rows[14]["resolution"] == "dismissed"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_comments (review_id, category, comment, resolution)"
            " VALUES (1, 'must_fix', 'should reject', 'deferred')"
        )

    conn.close()


def test_migrate_65_idempotent_when_already_at_v65(db_path, config_path):
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 65")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_65(db, config_path, SCRIPT_DIR)
    assert tusk_migrate.get_version(db) >= 65

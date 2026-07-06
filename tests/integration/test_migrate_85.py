"""Integration tests for migration 85: review comment spec-gap classification."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)

BIN_DIR = os.path.join(REPO_ROOT, "bin")


def _columns(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _user_version(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_migrate_85_adds_review_comment_spec_gap_type(tmp_path, config_path):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE review_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                file_path TEXT,
                line_start INTEGER,
                line_end INTEGER,
                category TEXT,
                severity TEXT,
                comment TEXT NOT NULL,
                resolution TEXT DEFAULT NULL,
                resolution_note TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            PRAGMA user_version = 84;
            """
        )
    finally:
        conn.close()

    assert "spec_gap_type" not in _columns(db_path, "review_comments")

    tusk_migrate.migrate_85(str(db_path), str(config_path), BIN_DIR)

    assert "spec_gap_type" in _columns(db_path, "review_comments")
    assert _user_version(db_path) == 85

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO review_comments (review_id, comment, spec_gap_type)"
            " VALUES (1, 'needs verification', 'missing_verification')"
        )
        conn.execute(
            "INSERT INTO review_comments (review_id, comment, spec_gap_type)"
            " VALUES (1, 'bad enum', 'not_a_gap')"
        )
    except sqlite3.IntegrityError as exc:
        assert "CHECK constraint failed" in str(exc)
    else:
        raise AssertionError("invalid spec_gap_type should violate CHECK constraint")
    finally:
        conn.close()


def test_fresh_init_is_at_or_past_v85_and_has_spec_gap_type(db_path):
    assert _user_version(db_path) >= 85
    assert "spec_gap_type" in _columns(db_path, "review_comments")

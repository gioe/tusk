"""Integration test for migrate_43: backfill normalize whitespace in convention topics.

When migrate_43 runs against a DB that has conventions with space-padded topics
(e.g. 'zsh, cli, git'), it must:
  1. Strip spaces around commas so 'zsh, cli, git' becomes 'zsh,cli,git'.
  2. Leave already-normalized topics unchanged.
  3. Leave NULL topics unchanged.
  4. Advance user_version to 43.
  5. Be idempotent — running it twice produces no errors and no additional changes.
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_at_v42(db_path):
    """Return a fully-initialised DB rewound to version 42 with test conventions."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 42")
    # Insert conventions with various topic formats to exercise all cases.
    conn.executemany(
        "INSERT INTO conventions (text, topics) VALUES (?, ?)",
        [
            ("space after comma",  "zsh, cli, git"),    # ', ' → ','
            ("space before comma", "zsh ,cli ,git"),    # ' ,' → ','
            ("both sides",         "zsh , cli , git"),  # both
            ("already clean",      "zsh,cli,git"),      # unchanged
            ("single topic",       "cli"),               # unchanged
            ("null topics",        None),                # unchanged
        ],
    )
    conn.commit()
    conn.close()
    return str(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMigrate43:

    def test_space_after_comma_normalized(self, db_at_v42, config_path):
        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v42)
        row = conn.execute(
            "SELECT topics FROM conventions WHERE text = 'space after comma'"
        ).fetchone()
        conn.close()
        assert row[0] == "zsh,cli,git"

    def test_space_before_comma_normalized(self, db_at_v42, config_path):
        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v42)
        row = conn.execute(
            "SELECT topics FROM conventions WHERE text = 'space before comma'"
        ).fetchone()
        conn.close()
        assert row[0] == "zsh,cli,git"

    def test_both_sides_normalized(self, db_at_v42, config_path):
        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v42)
        row = conn.execute(
            "SELECT topics FROM conventions WHERE text = 'both sides'"
        ).fetchone()
        conn.close()
        assert row[0] == "zsh,cli,git"

    def test_already_clean_unchanged(self, db_at_v42, config_path):
        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v42)
        row = conn.execute(
            "SELECT topics FROM conventions WHERE text = 'already clean'"
        ).fetchone()
        conn.close()
        assert row[0] == "zsh,cli,git"

    def test_single_topic_unchanged(self, db_at_v42, config_path):
        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v42)
        row = conn.execute(
            "SELECT topics FROM conventions WHERE text = 'single topic'"
        ).fetchone()
        conn.close()
        assert row[0] == "cli"

    def test_null_topics_unchanged(self, db_at_v42, config_path):
        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v42)
        row = conn.execute(
            "SELECT topics FROM conventions WHERE text = 'null topics'"
        ).fetchone()
        conn.close()
        assert row[0] is None

    def test_schema_version_advanced_to_43(self, db_at_v42, config_path):
        assert tusk_migrate.get_version(db_at_v42) == 42

        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(db_at_v42) == 43

    def test_idempotent(self, db_at_v42, config_path):
        """Running migrate_43 twice produces no errors and no additional changes."""
        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v42)
        before = conn.execute(
            "SELECT text, topics FROM conventions ORDER BY text"
        ).fetchall()
        conn.close()

        # Rewind version to simulate a second run attempt.
        conn = sqlite3.connect(db_at_v42)
        conn.execute("PRAGMA user_version = 42")
        conn.commit()
        conn.close()

        tusk_migrate.migrate_43(db_at_v42, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v42)
        after = conn.execute(
            "SELECT text, topics FROM conventions ORDER BY text"
        ).fetchall()
        conn.close()

        assert before == after

"""Integration test for migrate_64: add glossary table and seed from docs/GLOSSARY.md.

Covers:
- schema version advances 63 → 64
- glossary table is created with the documented column shape
- Entries are seeded from <repo_root>/docs/GLOSSARY.md when present
- Topics are applied to canonical seed terms
- No-op when GLOSSARY.md is absent (target projects)
- No-op when the table is already populated (preserves user state)
- Idempotent on re-run when already at v64
- The CLI subcommands (list/get/search/export) operate against the migrated DB
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")
TUSK_BIN = os.path.join(SCRIPT_DIR, "tusk")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate",
        os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


SAMPLE_GLOSSARY_MD = """\
# Tusk Glossary

Canonical definitions.

---

## chain head

A task that is ready to start.

→ See `v_chain_heads`.

---

## WSJF

Weighted Shortest Job First — priority scoring.

→ See [`DOMAIN.md`](DOMAIN.md#wsjf).
"""


@pytest.fixture()
def repo_with_glossary_md(tmp_path, config_path, monkeypatch):
    """Build a production-layout tmp repo: db at tmp/tusk/tasks.db and docs/GLOSSARY.md alongside.

    Migration 64 resolves <repo_root> via dirname(dirname(db_path)), so the
    DB must live at <repo_root>/tusk/tasks.db for the default markdown
    lookup to land on <repo_root>/docs/GLOSSARY.md.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(parents=True, exist_ok=True)
    db_file = tusk_dir / "tasks.db"

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "GLOSSARY.md").write_text(SAMPLE_GLOSSARY_MD, encoding="utf-8")

    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(db_file))
    conn.execute("DROP TABLE IF EXISTS glossary")
    conn.execute("PRAGMA user_version = 63")
    conn.commit()
    conn.close()
    return db_file


class TestMigrate64:

    def test_schema_version_advances_to_64(self, repo_with_glossary_md, config_path):
        assert tusk_migrate.get_version(str(repo_with_glossary_md)) == 63
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(str(repo_with_glossary_md)) == 64

    def test_glossary_table_created_with_expected_columns(
        self, repo_with_glossary_md, config_path
    ):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        conn = sqlite3.connect(str(repo_with_glossary_md))
        cols = [row[1] for row in conn.execute("PRAGMA table_info(glossary)").fetchall()]
        conn.close()
        assert set(cols) == {
            "id", "term", "definition", "see_also", "topics",
            "created_at", "updated_at",
        }

    def test_seeded_from_md(self, repo_with_glossary_md, config_path):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        conn = sqlite3.connect(str(repo_with_glossary_md))
        rows = conn.execute(
            "SELECT term, definition FROM glossary ORDER BY term COLLATE NOCASE"
        ).fetchall()
        conn.close()
        assert rows == [
            ("chain head", "A task that is ready to start."),
            ("WSJF", "Weighted Shortest Job First — priority scoring."),
        ]

    def test_canonical_terms_get_topics(self, repo_with_glossary_md, config_path):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        conn = sqlite3.connect(str(repo_with_glossary_md))
        topics_by_term = dict(
            conn.execute("SELECT term, topics FROM glossary").fetchall()
        )
        conn.close()
        assert topics_by_term["chain head"] == "chain,deps,view"
        assert topics_by_term["WSJF"] == "priority,wsjf,scoring"

    def test_no_op_when_glossary_md_missing(self, tmp_path, config_path, monkeypatch):
        tusk_dir = tmp_path / "tusk"
        tusk_dir.mkdir(parents=True, exist_ok=True)
        db_file = tusk_dir / "tasks.db"

        monkeypatch.setenv("TUSK_DB", str(db_file))
        subprocess.run(
            [TUSK_BIN, "init", "--force", "--skip-gitignore"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )

        conn = sqlite3.connect(str(db_file))
        conn.execute("DROP TABLE IF EXISTS glossary")
        conn.execute("PRAGMA user_version = 63")
        conn.commit()
        conn.close()

        tusk_migrate.migrate_64(str(db_file), config_path, SCRIPT_DIR)

        conn = sqlite3.connect(str(db_file))
        count = conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert count == 0
        assert version == 64

    def test_no_op_when_table_already_seeded(self, repo_with_glossary_md, config_path):
        # Seed a custom entry so the row-count guard treats the table as
        # already populated and skips the md import.
        conn = sqlite3.connect(str(repo_with_glossary_md))
        conn.execute("""
            CREATE TABLE glossary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term TEXT NOT NULL UNIQUE,
                definition TEXT NOT NULL,
                see_also TEXT,
                topics TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "INSERT INTO glossary (term, definition) VALUES ('custom', 'Existing.')"
        )
        conn.commit()
        conn.close()

        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)

        conn = sqlite3.connect(str(repo_with_glossary_md))
        rows = conn.execute("SELECT term FROM glossary").fetchall()
        conn.close()
        assert rows == [("custom",)]

    def test_idempotent_when_already_at_v64(self, repo_with_glossary_md, config_path):
        # Stamp at v64 directly — migration should short-circuit.
        conn = sqlite3.connect(str(repo_with_glossary_md))
        conn.execute("PRAGMA user_version = 64")
        conn.commit()
        conn.close()

        # Should not error and should not re-run any logic.
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)

        conn = sqlite3.connect(str(repo_with_glossary_md))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        # Table need not exist on the v64 path because we never created it.
        conn.close()
        assert version == 64


class TestGlossaryCli:
    """Smoke tests against the CLI surface after migration 64 has run."""

    def _run(self, db_file, *args):
        return subprocess.run(
            [TUSK_BIN, "glossary", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "TUSK_DB": str(db_file)},
        )

    def test_list_returns_seeded_entries(self, repo_with_glossary_md, config_path):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        result = self._run(repo_with_glossary_md, "list")
        assert result.returncode == 0
        assert "chain head" in result.stdout
        assert "WSJF" in result.stdout

    def test_get_returns_single_entry(self, repo_with_glossary_md, config_path):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        result = self._run(repo_with_glossary_md, "get", "WSJF")
        assert result.returncode == 0
        assert "Weighted Shortest" in result.stdout

    def test_get_unknown_term_exits_nonzero(self, repo_with_glossary_md, config_path):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        result = self._run(repo_with_glossary_md, "get", "nonexistent")
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_search_matches_topic(self, repo_with_glossary_md, config_path):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        result = self._run(repo_with_glossary_md, "search", "deps")
        assert result.returncode == 0
        # chain head has topics "chain,deps,view"; WSJF does not.
        assert "chain head" in result.stdout
        assert "WSJF" not in result.stdout

    def test_export_stdout_emits_generated_header(
        self, repo_with_glossary_md, config_path
    ):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        result = self._run(repo_with_glossary_md, "export", "--stdout")
        assert result.returncode == 0
        assert result.stdout.startswith(
            "<!-- generated by `tusk glossary export`"
        )
        assert "## chain head" in result.stdout
        assert "## WSJF" in result.stdout

    def test_add_then_remove_round_trip(self, repo_with_glossary_md, config_path):
        tusk_migrate.migrate_64(str(repo_with_glossary_md), config_path, SCRIPT_DIR)
        add_result = self._run(
            repo_with_glossary_md,
            "add",
            "test-term",
            "Test definition.",
            "--topics",
            "test, sample",
        )
        assert add_result.returncode == 0

        list_result = self._run(repo_with_glossary_md, "list")
        assert "test-term" in list_result.stdout

        remove_result = self._run(repo_with_glossary_md, "remove", "test-term")
        assert remove_result.returncode == 0

        get_result = self._run(repo_with_glossary_md, "get", "test-term")
        assert get_result.returncode == 1

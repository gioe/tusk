"""Integration test for migrate_47: backfill pillars from docs/PILLARS.md.

Covers the end-to-end contract consumed by `/investigate`, `/investigate-directory`,
and `/address-issue`: after migration 47 runs on a DB at v46 with a PILLARS.md at
<repo_root>/docs/PILLARS.md, `SELECT name, core_claim FROM pillars` returns the
pillars parsed from the doc, and `PRAGMA user_version` advances to 47.
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys

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


SAMPLE_PILLARS_MD = """\
# Sample Pillars

---

## Maturity Summary

| Pillar | Maturity |
|---|---|
| Alpha | High |

---

## 1. Alpha

**Definition:** First pillar.

**Core claim:** Alpha claim text.

---

## 2. Beta-Gamma

**Definition:** Hyphenated pillar name.

**Core claim:** Beta-gamma claim text.

---

## 3. Delta

**Definition:** No core claim below — should be skipped.

Representative features:
- None
"""


@pytest.fixture()
def repo_with_pillars_md(tmp_path, config_path, monkeypatch):
    """Build a production-layout tmp repo: db at tmp/tusk/tasks.db and docs/PILLARS.md alongside.

    Migration 47 resolves ``<repo_root>`` via ``dirname(dirname(db_path))``, so
    the DB must live at ``<repo_root>/tusk/tasks.db`` — matching how tusk lays
    out files in a real checkout — for the default markdown lookup to land on
    ``<repo_root>/docs/PILLARS.md``.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(parents=True, exist_ok=True)
    db_file = tusk_dir / "tasks.db"

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "PILLARS.md").write_text(SAMPLE_PILLARS_MD)

    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(db_file))
    conn.execute("DELETE FROM pillars")
    conn.execute("PRAGMA user_version = 46")
    conn.commit()
    conn.close()
    return db_file


class TestMigrate47:

    def test_schema_version_advances_to_47(self, repo_with_pillars_md, config_path):
        assert tusk_migrate.get_version(str(repo_with_pillars_md)) == 46
        tusk_migrate.migrate_47(str(repo_with_pillars_md), config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(str(repo_with_pillars_md)) == 47

    def test_pillars_backfilled_from_md(self, repo_with_pillars_md, config_path):
        tusk_migrate.migrate_47(str(repo_with_pillars_md), config_path, SCRIPT_DIR)

        conn = sqlite3.connect(str(repo_with_pillars_md))
        rows = conn.execute(
            "SELECT name, core_claim FROM pillars ORDER BY id"
        ).fetchall()
        conn.close()

        # Alpha and Beta-Gamma parse; Delta is skipped (no core claim line).
        assert rows == [
            ("Alpha", "Alpha claim text."),
            ("Beta-Gamma", "Beta-gamma claim text."),
        ]

    def test_no_op_when_pillars_md_missing(self, tmp_path, config_path, monkeypatch):
        # Build a production-layout tmp repo with NO docs/PILLARS.md.
        tusk_dir = tmp_path / "tusk"
        tusk_dir.mkdir(parents=True, exist_ok=True)
        db_file = tusk_dir / "tasks.db"

        monkeypatch.setenv("TUSK_DB", str(db_file))
        subprocess.run(
            [TUSK_BIN, "init", "--force", "--skip-gitignore"],
            capture_output=True,
            text=True,
            check=True,
        )

        conn = sqlite3.connect(str(db_file))
        conn.execute("DELETE FROM pillars")
        conn.execute("PRAGMA user_version = 46")
        conn.commit()
        conn.close()

        tusk_migrate.migrate_47(str(db_file), config_path, SCRIPT_DIR)

        conn = sqlite3.connect(str(db_file))
        count = conn.execute("SELECT COUNT(*) FROM pillars").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert count == 0
        assert version == 47

    def test_no_op_when_table_already_seeded(self, repo_with_pillars_md, config_path):
        # Pre-populate with a custom pillar — migration should leave it alone.
        conn = sqlite3.connect(str(repo_with_pillars_md))
        conn.execute(
            "INSERT INTO pillars (name, core_claim) VALUES ('Custom', 'Existing claim')"
        )
        conn.commit()
        conn.close()

        tusk_migrate.migrate_47(str(repo_with_pillars_md), config_path, SCRIPT_DIR)

        conn = sqlite3.connect(str(repo_with_pillars_md))
        rows = conn.execute(
            "SELECT name, core_claim FROM pillars ORDER BY id"
        ).fetchall()
        conn.close()
        assert rows == [("Custom", "Existing claim")]

    def test_idempotent(self, repo_with_pillars_md, config_path):
        tusk_migrate.migrate_47(str(repo_with_pillars_md), config_path, SCRIPT_DIR)

        # Rewind version only — leave pillars in place — and re-run.
        conn = sqlite3.connect(str(repo_with_pillars_md))
        conn.execute("PRAGMA user_version = 46")
        conn.commit()
        conn.close()

        tusk_migrate.migrate_47(str(repo_with_pillars_md), config_path, SCRIPT_DIR)

        conn = sqlite3.connect(str(repo_with_pillars_md))
        rows = conn.execute(
            "SELECT name, core_claim FROM pillars ORDER BY id"
        ).fetchall()
        conn.close()
        # Table was non-empty on the second run, so no re-seeding occurred.
        assert rows == [
            ("Alpha", "Alpha claim text."),
            ("Beta-Gamma", "Beta-gamma claim text."),
        ]


class TestInvestigateFilterReady:
    """After migration, the JSON shape consumed by /investigate is non-empty."""

    def test_tusk_pillars_list_returns_parsed_pillars(
        self, repo_with_pillars_md, config_path, monkeypatch
    ):
        tusk_migrate.migrate_47(str(repo_with_pillars_md), config_path, SCRIPT_DIR)

        monkeypatch.setenv("TUSK_DB", str(repo_with_pillars_md))
        result = subprocess.run(
            [TUSK_BIN, "pillars", "list"],
            capture_output=True,
            text=True,
            check=True,
        )
        import json as _json

        payload = _json.loads(result.stdout)
        names = [p["name"] for p in payload]
        assert names == ["Alpha", "Beta-Gamma"]
        # Every entry exposes the two keys /investigate filters on.
        assert all("core_claim" in p and "name" in p for p in payload)

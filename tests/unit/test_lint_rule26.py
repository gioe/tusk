"""Unit tests for rule26_glossary_drift in tusk-lint.py.

Tests the pass case (file matches table), the violation case (file diverges
from table), and the skip cases (no DB, no GLOSSARY.md, no glossary table,
empty glossary table).
"""

import importlib.util
import os
import sqlite3
import tempfile
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint",
    os.path.join(REPO_ROOT, "bin", "tusk-lint.py"),
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def _make_glossary_db(tmp_dir, rows=None):
    """Create a minimal SQLite DB with a populated glossary table.

    rows: list of (term, definition, see_also, topics) tuples.
    """
    db_path = os.path.join(tmp_dir, "tasks.db")
    conn = sqlite3.connect(db_path)
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
    for term, definition, see_also, topics in (rows or []):
        conn.execute(
            "INSERT INTO glossary (term, definition, see_also, topics) "
            "VALUES (?, ?, ?, ?)",
            (term, definition, see_also, topics),
        )
    conn.commit()
    conn.close()
    return db_path


def _write_glossary_md(tmp_dir, content):
    docs_dir = os.path.join(tmp_dir, "docs")
    os.makedirs(docs_dir, exist_ok=True)
    md_path = os.path.join(docs_dir, "GLOSSARY.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)
    return md_path


class TestRule26NoViolations:

    def test_no_db_path_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lint, "_db_path_from_root", return_value=None):
                assert lint.rule26_glossary_drift(tmp) == []

    def test_no_glossary_md_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_glossary_db(tmp, rows=[("term", "def", None, None)])
            # No docs/GLOSSARY.md on disk.
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule26_glossary_drift(tmp) == []

    def test_empty_glossary_table_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_glossary_db(tmp, rows=[])
            _write_glossary_md(tmp, "# anything\n")
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule26_glossary_drift(tmp) == []

    def test_missing_glossary_table_returns_empty(self):
        """Pre-v64 DB without the glossary table — must skip cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "tasks.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE other_table (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
            _write_glossary_md(tmp, "# anything\n")
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule26_glossary_drift(tmp) == []

    def test_file_matches_table_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                ("alpha", "First definition.", "[`X`](x.md)", "tag1"),
                ("beta", "Second.", None, None),
            ]
            db_path = _make_glossary_db(tmp, rows=rows)

            # Render via the same code path the lint rule uses.
            import sys as _sys
            _sys.path.insert(0, os.path.join(REPO_ROOT, "bin"))
            spec = importlib.util.spec_from_file_location(
                "tusk_glossary",
                os.path.join(REPO_ROOT, "bin", "tusk-glossary.py"),
            )
            g = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(g)
            expected = g.render_glossary_md(rows)
            _write_glossary_md(tmp, expected)

            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule26_glossary_drift(tmp) == []


class TestRule26Violations:

    def test_definition_drift_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_glossary_db(
                tmp,
                rows=[("alpha", "Authoritative definition.", None, None)],
            )
            _write_glossary_md(tmp, "## alpha\n\nStale hand-edited definition.\n")
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule26_glossary_drift(tmp)
        assert len(violations) >= 1
        assert any("out of sync" in v for v in violations)
        assert any("tusk glossary export" in v for v in violations)

    def test_extra_term_in_md_flagged(self):
        """File contains a term that's missing from the table."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_glossary_db(
                tmp,
                rows=[("alpha", "First.", None, None)],
            )
            _write_glossary_md(
                tmp,
                "## alpha\n\nFirst.\n\n---\n\n## ghost\n\nNot in the table.\n",
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule26_glossary_drift(tmp)
        assert violations  # any drift triggers the rule

    def test_missing_term_in_md_flagged(self):
        """Table has a term that's missing from the file."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_glossary_db(
                tmp,
                rows=[
                    ("alpha", "First.", None, None),
                    ("beta", "Second.", None, None),
                ],
            )
            # File only covers alpha — beta is in the table but not the file.
            _write_glossary_md(tmp, "## alpha\n\nFirst.\n")
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule26_glossary_drift(tmp)
        assert violations

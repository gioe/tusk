"""Unit tests for `tusk lint-rule propose` (Task 712).

`tusk lint-rule propose` converts a grep-detectable anti-pattern surfaced by
/retro into a *staged advisory* lint rule and records which retro finding it
came from. It is the auto-propose path built on top of the advisory tier added
in Task 711 (`enforcement='advisory'`).

Three invariants, one per acceptance criterion:

* ``stages_advisory`` (#3326) — a proposed rule is inserted
  ``enforcement='advisory'`` (and ``is_blocking=1``), reusing the advisory
  staging path so it warns but is never gating-by-default.
* ``provenance`` (#3327) — the originating retro finding id is recorded in
  ``lint_rules.source_finding_id``, and a bad finding id is rejected.
* ``no_gate`` (#3328) — because it is advisory, a proposed rule does NOT load
  into the gating bucket (Rule 16), so it cannot fail the lint gate that backs
  ``tusk merge``.
"""

import importlib.util
import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_lint_spec = importlib.util.spec_from_file_location(
    "tusk_lint", os.path.join(BIN, "tusk-lint.py")
)
lint = importlib.util.module_from_spec(_lint_spec)
_lint_spec.loader.exec_module(lint)

_rules_spec = importlib.util.spec_from_file_location(
    "tusk_lint_rules", os.path.join(BIN, "tusk-lint-rules.py")
)
rules_mod = importlib.util.module_from_spec(_rules_spec)
_rules_spec.loader.exec_module(rules_mod)


# Mirror the post-migration lint_rules schema (bin/tusk cmd_init + migrations
# 82 and 83). source_finding_id carries provenance back to retro_findings.
_LINT_RULES_SCHEMA = """
CREATE TABLE lint_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grep_pattern TEXT NOT NULL,
    file_glob TEXT NOT NULL,
    message TEXT NOT NULL,
    is_blocking INTEGER NOT NULL DEFAULT 0,
    source_skill TEXT,
    enforcement TEXT NOT NULL DEFAULT 'enforcing',
    source_finding_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    CHECK (is_blocking IN (0, 1)),
    CHECK (enforcement IN ('advisory', 'enforcing'))
);
"""

# A minimal retro_findings table so --finding-id can be validated as a real FK.
_RETRO_FINDINGS_SCHEMA = """
CREATE TABLE retro_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_run_id INTEGER NOT NULL,
    task_id INTEGER,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    action_taken TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# A pattern that matches a sentinel line we plant in a tmp file.
_PATTERN = "FORBIDDEN_TOKEN"
_FLAGGED_LINE = "x = FORBIDDEN_TOKEN  # planted\n"


def _make_db(tmp_path, *, with_finding=False) -> str:
    """Create a tmp DB with the lint_rules + retro_findings tables.

    When ``with_finding`` is set, seed one retro_findings row (id 7) so a
    propose call can reference it.
    """
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_LINT_RULES_SCHEMA)
    conn.executescript(_RETRO_FINDINGS_SCHEMA)
    if with_finding:
        conn.execute(
            "INSERT INTO retro_findings (id, skill_run_id, category, summary)"
            " VALUES (7, 100, 'process', 'observed anti-pattern X')"
        )
    conn.commit()
    conn.close()
    return db_path


def _make_tree_with_violation(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "offender.txt").write_text(_FLAGGED_LINE, encoding="utf-8")
    return str(root)


def _patch_db(monkeypatch, db_path):
    monkeypatch.setattr(lint, "_db_path_from_root", lambda root: db_path)


def _row(db_path, rule_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM lint_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    finally:
        conn.close()


def _max_id(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT MAX(id) FROM lint_rules").fetchone()[0]
    finally:
        conn.close()


def _propose(db_path, *args) -> int:
    argv = ["tusk-lint-rules.py", db_path, "/dev/null", "propose", *args]
    return rules_mod.main(argv)


class TestStagesAdvisory:
    def test_propose_stages_advisory_rule(self, tmp_path, capsys):
        """A proposed rule is inserted enforcement='advisory' and is_blocking=1."""
        db = _make_db(tmp_path)
        assert _propose(db, _PATTERN, "**/*.txt", "no forbidden token") == 0
        new_id = _max_id(db)
        # The lastrowid is echoed for the caller to capture.
        assert str(new_id) in capsys.readouterr().out

        row = _row(db, new_id)
        assert row["enforcement"] == "advisory"
        assert row["is_blocking"] == 1
        assert row["grep_pattern"] == _PATTERN
        assert row["file_glob"] == "**/*.txt"
        assert row["message"] == "no forbidden token"

    def test_propose_defaults_source_skill_to_retro(self, tmp_path):
        db = _make_db(tmp_path)
        assert _propose(db, _PATTERN, "**/*.txt", "msg") == 0
        assert _row(db, _max_id(db))["source_skill"] == "retro"


class TestProvenance:
    def test_propose_records_source_finding_provenance(self, tmp_path):
        """--finding-id is stored in source_finding_id for provenance back to
        the originating retro finding."""
        db = _make_db(tmp_path, with_finding=True)
        assert _propose(db, _PATTERN, "**/*.txt", "msg", "--finding-id", "7") == 0
        row = _row(db, _max_id(db))
        assert row["source_finding_id"] == 7

    def test_propose_without_finding_id_leaves_provenance_null(self, tmp_path):
        db = _make_db(tmp_path)
        assert _propose(db, _PATTERN, "**/*.txt", "msg") == 0
        assert _row(db, _max_id(db))["source_finding_id"] is None

    def test_propose_unknown_finding_id_rejected(self, tmp_path, capsys):
        """A finding id that does not exist fails fast (exit 2) and inserts
        nothing — provenance must reference a real finding."""
        db = _make_db(tmp_path, with_finding=True)
        assert _propose(db, _PATTERN, "**/*.txt", "msg", "--finding-id", "999") == 2
        err = capsys.readouterr().err
        assert "999" in err
        assert "not found" in err
        assert _max_id(db) is None  # nothing inserted


class TestNoGate:
    def test_proposed_rule_no_gate(self, tmp_path, monkeypatch):
        """Because it is advisory, a proposed rule must NOT load into the gating
        bucket (Rule 16) — so it cannot fail the lint gate that backs tusk
        merge, even though it is is_blocking=1."""
        db = _make_db(tmp_path)
        assert _propose(db, _PATTERN, "**/*.txt", "no forbidden token") == 0
        _patch_db(monkeypatch, db)
        tree = _make_tree_with_violation(tmp_path)

        gating = lint._load_lint_rules(tree, gating=True)
        assert gating == [], "proposed advisory rule must not be in the gating bucket"
        assert lint.rule16_db_rules_blocking(tree) == []

    def test_proposed_rule_still_warns(self, tmp_path, monkeypatch):
        """Control: the proposed rule still surfaces as an advisory warning
        (Rule 17), so it is observable before promotion."""
        db = _make_db(tmp_path)
        assert _propose(db, _PATTERN, "**/*.txt", "no forbidden token") == 0
        _patch_db(monkeypatch, db)
        tree = _make_tree_with_violation(tmp_path)

        warn_rules = lint._load_lint_rules(tree, gating=False)
        assert len(warn_rules) == 1
        violations = lint.rule17_db_rules_advisory(tree)
        assert len(violations) == 1
        assert "offender.txt" in violations[0]
        assert "no forbidden token" in violations[0]

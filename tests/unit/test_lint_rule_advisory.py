"""Unit tests for the advisory enforcement tier of DB-backed lint rules (Task 711).

Two orthogonal columns drive the warn-vs-gate decision on a ``lint_rules`` row:

* ``is_blocking`` — the long-standing distinction between gating (Rule 16) and
  advisory (Rule 17) candidates.
* ``enforcement`` — ``'advisory'`` stages a rule for observation so its hits
  warn but never gate, even when ``is_blocking=1``; ``'enforcing'`` lets a
  blocking rule gate the lint exit code.

A rule gates only when ``is_blocking = 1 AND enforcement = 'enforcing'``.

Coverage:
* ``advisory_no_gate`` — a blocking rule staged ``enforcement='advisory'`` does
  not flow into the gating bucket (Rule 16), so it cannot fail the lint gate.
* ``advisory_warns`` — that same advisory rule still flows into the warn-only
  bucket (Rule 17), so its hits are reported as warnings.
* ``promote`` — ``tusk lint-rule promote`` flips advisory → enforcing (and is a
  no-op / error on the edge cases).
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


# Mirror the post-migration lint_rules schema (bin/tusk cmd_init + migration 82).
_LINT_RULES_SCHEMA = """
CREATE TABLE lint_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grep_pattern TEXT NOT NULL,
    file_glob TEXT NOT NULL,
    message TEXT NOT NULL,
    is_blocking INTEGER NOT NULL DEFAULT 0,
    source_skill TEXT,
    enforcement TEXT NOT NULL DEFAULT 'enforcing',
    created_at TEXT DEFAULT (datetime('now')),
    CHECK (is_blocking IN (0, 1)),
    CHECK (enforcement IN ('advisory', 'enforcing'))
);
"""

# A pattern that matches a sentinel line we plant in a tmp file.
_PATTERN = "FORBIDDEN_TOKEN"
_FLAGGED_LINE = "x = FORBIDDEN_TOKEN  # planted\n"


def _make_db(tmp_path, *, is_blocking, enforcement) -> str:
    """Create a tmp DB seeded with one lint_rule row and return its path."""
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_LINT_RULES_SCHEMA)
    conn.execute(
        "INSERT INTO lint_rules"
        " (id, grep_pattern, file_glob, message, is_blocking, source_skill, enforcement)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (5, _PATTERN, "**/*.txt", "forbidden token present", is_blocking,
         "retro", enforcement),
    )
    conn.commit()
    conn.close()
    return db_path


def _make_tree_with_violation(tmp_path):
    """Create a source tree with one file containing the flagged line."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "offender.txt").write_text(_FLAGGED_LINE, encoding="utf-8")
    return str(root)


def _patch_db(monkeypatch, db_path):
    """Force tusk-lint's DB resolver to point at our seeded tmp DB."""
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


def _promote(db_path, *args) -> int:
    argv = ["tusk-lint-rules.py", db_path, "/dev/null", "promote", *args]
    return rules_mod.main(argv)


def _add(db_path, *args) -> int:
    argv = ["tusk-lint-rules.py", db_path, "/dev/null", "add", *args]
    return rules_mod.main(argv)


class TestAdvisoryNoGate:
    def test_advisory_no_gate_blocking_rule_excluded_from_gating_bucket(
        self, tmp_path, monkeypatch
    ):
        """A blocking rule staged advisory must NOT load into the gating bucket
        (Rule 16), so it cannot push the lint exit code non-zero."""
        db = _make_db(tmp_path, is_blocking=1, enforcement="advisory")
        _patch_db(monkeypatch, db)
        tree = _make_tree_with_violation(tmp_path)

        gating = lint._load_lint_rules(tree, gating=True)
        assert gating == [], "advisory rule must not appear in the gating bucket"

        # And running the gating rule against a tree that DOES violate it yields
        # zero gating violations — the gate stays green.
        assert lint.rule16_db_rules_blocking(tree) == []

    def test_enforcing_blocking_rule_still_gates(self, tmp_path, monkeypatch):
        """Control: a blocking + enforcing rule still gates (regression guard)."""
        db = _make_db(tmp_path, is_blocking=1, enforcement="enforcing")
        _patch_db(monkeypatch, db)
        tree = _make_tree_with_violation(tmp_path)

        gating = lint._load_lint_rules(tree, gating=True)
        assert len(gating) == 1

        violations = lint.rule16_db_rules_blocking(tree)
        assert len(violations) == 1
        assert "offender.txt" in violations[0]


class TestAdvisoryWarns:
    def test_advisory_warns_blocking_rule_reported_in_advisory_bucket(
        self, tmp_path, monkeypatch
    ):
        """The advisory rule's hits must still be reported — they land in the
        warn-only bucket (Rule 17) instead of the gating bucket."""
        db = _make_db(tmp_path, is_blocking=1, enforcement="advisory")
        _patch_db(monkeypatch, db)
        tree = _make_tree_with_violation(tmp_path)

        warn_rules = lint._load_lint_rules(tree, gating=False)
        assert len(warn_rules) == 1, "advisory rule must appear in the warn bucket"

        violations = lint.rule17_db_rules_advisory(tree)
        assert len(violations) == 1
        assert "offender.txt" in violations[0]
        assert "forbidden token present" in violations[0]

    def test_enforcing_rule_not_double_counted_in_advisory_bucket(
        self, tmp_path, monkeypatch
    ):
        """A blocking + enforcing rule must NOT also appear in the warn bucket,
        or its hits would be counted twice."""
        db = _make_db(tmp_path, is_blocking=1, enforcement="enforcing")
        _patch_db(monkeypatch, db)
        tree = _make_tree_with_violation(tmp_path)

        assert lint._load_lint_rules(tree, gating=False) == []
        assert lint.rule17_db_rules_advisory(tree) == []

    def test_non_blocking_rule_still_advisory(self, tmp_path, monkeypatch):
        """Backwards compat: an is_blocking=0 rule remains warn-only regardless
        of enforcement."""
        db = _make_db(tmp_path, is_blocking=0, enforcement="enforcing")
        _patch_db(monkeypatch, db)
        tree = _make_tree_with_violation(tmp_path)

        assert lint._load_lint_rules(tree, gating=True) == []
        warn_rules = lint._load_lint_rules(tree, gating=False)
        assert len(warn_rules) == 1


class TestPromote:
    def test_promote_flips_advisory_to_enforcing(self, tmp_path, capsys):
        db = _make_db(tmp_path, is_blocking=1, enforcement="advisory")
        assert _promote(db, "5") == 0
        out = capsys.readouterr().out
        assert "5" in out
        assert "enforcing" in out
        assert _row(db, 5)["enforcement"] == "enforcing"

    def test_promote_after_add_advisory_round_trip(self, tmp_path):
        """An `add --advisory` rule starts advisory and becomes enforcing on
        promote (end-to-end through the public CLI surface)."""
        db = str(tmp_path / "tasks.db")
        conn = sqlite3.connect(db)
        conn.executescript(_LINT_RULES_SCHEMA)
        conn.commit()
        conn.close()

        assert _add(db, _PATTERN, "**/*.txt", "msg", "--blocking", "--advisory") == 0
        new_id = _row_max_id(db)
        assert _row(db, new_id)["enforcement"] == "advisory"
        assert _row(db, new_id)["is_blocking"] == 1

        assert _promote(db, str(new_id)) == 0
        assert _row(db, new_id)["enforcement"] == "enforcing"

    def test_promote_already_enforcing_is_noop(self, tmp_path, capsys):
        db = _make_db(tmp_path, is_blocking=1, enforcement="enforcing")
        assert _promote(db, "5") == 0
        assert "already enforcing" in capsys.readouterr().out
        assert _row(db, 5)["enforcement"] == "enforcing"

    def test_promote_unknown_id_exits_2(self, tmp_path, capsys):
        db = _make_db(tmp_path, is_blocking=1, enforcement="advisory")
        assert _promote(db, "99999") == 2
        err = capsys.readouterr().err
        assert "99999" in err
        assert "not found" in err

    def test_add_default_enforcement_is_enforcing(self, tmp_path):
        """Without --advisory, a rule is added enforcing (default behavior
        unchanged)."""
        db = str(tmp_path / "tasks.db")
        conn = sqlite3.connect(db)
        conn.executescript(_LINT_RULES_SCHEMA)
        conn.commit()
        conn.close()

        assert _add(db, _PATTERN, "**/*.txt", "msg", "--blocking") == 0
        new_id = _row_max_id(db)
        assert _row(db, new_id)["enforcement"] == "enforcing"


def _row_max_id(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT MAX(id) FROM lint_rules").fetchone()[0]
    finally:
        conn.close()

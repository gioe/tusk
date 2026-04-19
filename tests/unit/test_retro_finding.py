"""Unit tests for tusk-retro-finding.py.

Covers (per TASK-113 criterion 492):
- happy path: all flags supplied → row inserted with every field populated
- --action-taken omitted → action_taken column stored as NULL (true SQL NULL,
  not a quoted string — the footgun that motivated the wrapper in the first
  place)
- --task-id omitted → task_id column stored as NULL
- unknown skill_run_id → exit 1, no row inserted (criterion 490 regression guard)
- unknown task_id → exit 1, no row inserted
- empty --category / empty --summary → exit 1

The fixture schema is a minimal subset of bin/tusk's canonical CREATE TABLE —
retro_findings + skill_runs + tasks only. No schema-sync guard targets
retro_findings; mirroring test_retro_themes.py's convention.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_retro_finding",
    os.path.join(BIN, "tusk-retro-finding.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── schema fixture ────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT
);
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


def _make_db(tmp_path):
    db_path = str(tmp_path / "findings.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO skill_runs (id, skill_name) VALUES (1, 'retro')")
    conn.execute("INSERT INTO tasks (id, summary) VALUES (42, 'parent task')")
    conn.commit()
    return db_path, conn


def _run_cli(db_path, *cli_args, config_path="fake.json"):
    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-retro-finding.py"),
         db_path, config_path, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


class TestAddFinding:
    """Direct unit tests against add_finding() — no subprocess overhead."""

    def test_happy_path_inserts_row_with_all_fields(self, tmp_path):
        db_path, conn = _make_db(tmp_path)

        row = mod.add_finding(
            conn,
            skill_run_id=1,
            task_id=42,
            category="A",
            summary="test finding",
            action_taken="task:TASK-99",
        )

        assert row["skill_run_id"] == 1
        assert row["task_id"] == 42
        assert row["category"] == "A"
        assert row["summary"] == "test finding"
        assert row["action_taken"] == "task:TASK-99"
        assert row["created_at"] is not None
        # Confirm the row actually landed in the table.
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM retro_findings"
        ).fetchone()["c"] == 1

    def test_action_taken_omitted_stores_real_null(self, tmp_path):
        """--action-taken omitted must land a true SQL NULL — not the literal
        string 'NULL' or an empty string. This is one of the two footguns the
        wrapper exists to eliminate (the raw INSERT required callers to
        remember $(tusk sql-quote '<value>') vs. bare NULL)."""
        db_path, conn = _make_db(tmp_path)

        row = mod.add_finding(
            conn,
            skill_run_id=1,
            task_id=42,
            category="A",
            summary="no action",
        )

        assert row["action_taken"] is None
        # Confirm at the SQL layer too — rules out a display-only coercion.
        stored = conn.execute(
            "SELECT action_taken FROM retro_findings WHERE id = ?", (row["id"],)
        ).fetchone()
        assert stored["action_taken"] is None

    def test_task_id_omitted_stores_real_null(self, tmp_path):
        """--task-id omitted must land a true SQL NULL, not a string. This is
        the second (and historically more dangerous) footgun — the raw INSERT
        used `<RETRO_TASK_ID or NULL>` as a literal substitution, so a missed
        NULL token would have silently stored the string 'NULL' as task_id."""
        db_path, conn = _make_db(tmp_path)

        row = mod.add_finding(
            conn,
            skill_run_id=1,
            category="A",
            summary="no task",
            action_taken="documented",
        )

        assert row["task_id"] is None
        stored = conn.execute(
            "SELECT task_id FROM retro_findings WHERE id = ?", (row["id"],)
        ).fetchone()
        assert stored["task_id"] is None

    def test_unknown_skill_run_id_raises_before_insert(self, tmp_path):
        """Criterion 490: the wrapper must reject an unknown skill_run_id
        before the INSERT runs, so a dangling-FK row never lands."""
        db_path, conn = _make_db(tmp_path)

        try:
            mod.add_finding(
                conn,
                skill_run_id=999,
                category="A",
                summary="should not land",
            )
            raised = False
        except ValueError as e:
            raised = True
            assert "999" in str(e)

        assert raised
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM retro_findings"
        ).fetchone()["c"] == 0

    def test_unknown_task_id_raises_before_insert(self, tmp_path):
        """Same guarantee for the optional task_id FK — a typo should fail
        fast and not land a row."""
        db_path, conn = _make_db(tmp_path)

        try:
            mod.add_finding(
                conn,
                skill_run_id=1,
                task_id=999,
                category="A",
                summary="should not land",
            )
            raised = False
        except ValueError as e:
            raised = True
            assert "999" in str(e)

        assert raised
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM retro_findings"
        ).fetchone()["c"] == 0


class TestMainCLI:
    """Subprocess tests for argparse + end-to-end exit-code behavior."""

    def test_full_flags_round_trip_via_cli(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.close()

        returncode, stdout, stderr = _run_cli(
            db_path, "add",
            "--skill-run-id", "1",
            "--task-id", "42",
            "--category", "B",
            "--summary", "cli round trip",
            "--action-taken", "issue:https://example.com/issues/1",
        )

        assert returncode == 0, stderr
        data = json.loads(stdout)
        assert data["skill_run_id"] == 1
        assert data["task_id"] == 42
        assert data["category"] == "B"
        assert data["summary"] == "cli round trip"
        assert data["action_taken"] == "issue:https://example.com/issues/1"

    def test_cli_omits_action_taken_and_task_id(self, tmp_path):
        """End-to-end confirmation that omitting the optional flags produces
        real NULLs on the JSON output side too (json null, not string 'null')."""
        db_path, conn = _make_db(tmp_path)
        conn.close()

        returncode, stdout, stderr = _run_cli(
            db_path, "add",
            "--skill-run-id", "1",
            "--category", "C",
            "--summary", "minimal invocation",
        )

        assert returncode == 0, stderr
        data = json.loads(stdout)
        assert data["task_id"] is None
        assert data["action_taken"] is None

    def test_cli_rejects_unknown_skill_run_id(self, tmp_path):
        db_path, _ = _make_db(tmp_path)

        returncode, stdout, stderr = _run_cli(
            db_path, "add",
            "--skill-run-id", "999",
            "--category", "A",
            "--summary", "ghost run",
        )

        assert returncode == 1
        assert "skill_run_id" in stderr
        assert stdout == ""

    def test_cli_rejects_empty_category(self, tmp_path):
        db_path, _ = _make_db(tmp_path)

        returncode, _, stderr = _run_cli(
            db_path, "add",
            "--skill-run-id", "1",
            "--category", "   ",
            "--summary", "whitespace cat",
        )

        assert returncode == 1
        assert "category" in stderr

    def test_cli_rejects_empty_summary(self, tmp_path):
        db_path, _ = _make_db(tmp_path)

        returncode, _, stderr = _run_cli(
            db_path, "add",
            "--skill-run-id", "1",
            "--category", "A",
            "--summary", "",
        )

        # argparse itself rejects empty required string via required=True?
        # No — required=True only checks presence, not emptiness. Our own
        # strip() check returns 1.
        assert returncode == 1
        assert "summary" in stderr

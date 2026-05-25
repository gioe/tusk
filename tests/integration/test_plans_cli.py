"""Integration tests for `tusk plans set|list|end` (issue #873).

Spins up a real DB via the db_path fixture, drives the CLI through the
tusk wrapper subprocess, and asserts on JSON output. Mirrors the
conventions/glossary integration-test pattern.
"""

import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(*args, expect_exit=0):
    result = subprocess.run(
        [TUSK_BIN, "plans", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == expect_exit, (
        f"tusk plans {' '.join(args)} exited {result.returncode}, expected {expect_exit}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return result


def test_set_then_list_json_round_trip(db_path):
    """`tusk plans set` inserts a row; `tusk plans list --format json`
    returns it with the canonical fields."""
    _run("set", "claude_max_20x", "200", "--effective-from", "2026-01-01")
    result = _run("list", "--format", "json")
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "claude_max_20x"
    assert row["monthly_cost_dollars"] == 200.0
    assert row["effective_from"] == "2026-01-01"
    assert row["effective_to"] is None


def test_set_rejects_negative_cost(db_path):
    """Negative monthly cost is blocked at the CLI layer (mirrors the DB
    CHECK constraint added by migration 71)."""
    _run("set", "bad_plan", "-50", expect_exit=1)


def test_list_active_on_filters_to_date(db_path):
    """`--active-on` returns only plans whose [effective_from, effective_to)
    window covers the target date."""
    _run("set", "old_plan", "100", "--effective-from", "2025-01-01")
    _run("end", "old_plan", "--effective-to", "2025-12-31")
    _run("set", "current", "200", "--effective-from", "2026-01-01")

    result = _run("list", "--format", "json", "--active-on", "2025-06-15")
    names = [r["name"] for r in json.loads(result.stdout)]
    assert names == ["old_plan"]

    result = _run("list", "--format", "json", "--active-on", "2026-05-15")
    names = [r["name"] for r in json.loads(result.stdout)]
    assert names == ["current"]


def test_end_closes_open_period(db_path):
    """`tusk plans end <name>` stamps effective_to on the most-recent
    open period for that name."""
    _run("set", "claude_max", "200", "--effective-from", "2026-01-01")
    _run("end", "claude_max", "--effective-to", "2026-04-01")

    result = _run("list", "--format", "json", "--name", "claude_max")
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["effective_to"] == "2026-04-01"


def test_end_refuses_when_no_open_period(db_path):
    """If every period for <name> is already closed (or no period
    exists), `tusk plans end` exits non-zero rather than silently
    no-op-ing."""
    _run("end", "nonexistent", expect_exit=2)

    _run("set", "claude_max", "200", "--effective-from", "2026-01-01")
    _run("end", "claude_max", "--effective-to", "2026-04-01")
    # Second end on the same name has no open period left.
    _run("end", "claude_max", expect_exit=2)


def test_set_then_set_again_records_history(db_path):
    """Successive `tusk plans set` calls record a price-change history;
    the name is intentionally non-unique."""
    _run("set", "claude_max", "200", "--effective-from", "2026-01-01")
    _run("end", "claude_max", "--effective-to", "2026-03-01")
    _run("set", "claude_max", "250", "--effective-from", "2026-03-01")

    result = _run("list", "--format", "json", "--name", "claude_max")
    rows = json.loads(result.stdout)
    assert len(rows) == 2
    # Ordered by effective_from ascending.
    assert rows[0]["monthly_cost_dollars"] == 200.0
    assert rows[1]["monthly_cost_dollars"] == 250.0
    assert rows[1]["effective_to"] is None


def test_list_text_format_renders_header(db_path):
    """Default text format prints a human header row."""
    _run("set", "claude_max", "200", "--effective-from", "2026-01-01")
    result = _run("list")
    assert "ID" in result.stdout
    assert "Monthly" in result.stdout
    assert "claude_max" in result.stdout


def test_list_empty_message(db_path):
    """Empty DB prints a hint pointing at `tusk plans set`."""
    result = _run("list")
    assert "tusk plans set" in result.stdout

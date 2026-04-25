"""End-to-end coverage for `tusk groom` JSON shape on a populated DB.

Criterion 682 from TASK-156: "Unit test covers the JSON shape on a populated
test DB including expired + unassigned + duplicate cases." An integration
test is the honest home — `tusk groom` is a thin orchestrator that shells
out to `tusk autoclose`, `tusk backlog-scan`, and `tusk lint`, so unit
isolation would replace the pipeline under test with mocks.
"""

import json
import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

JSON_KEYS = {
    "dry_run",
    "expired",
    "duplicates",
    "unassigned",
    "unsized",
    "autoclose_candidates",
    "lint",
}
AUTOCLOSE_KEYS = {"applied", "expired_deferred", "moot_contingent", "total"}


def _run_groom(db_path, *flags):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    return subprocess.run(
        [TUSK_BIN, "groom", *flags],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def _insert_task(
    conn,
    *,
    summary,
    status="To Do",
    complexity=None,
    assignee=None,
    is_deferred=0,
    expires_at=None,
):
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, "
        "priority_score, assignee, is_deferred, expires_at) "
        "VALUES (?, ?, 'feature', 'Medium', ?, 50, ?, ?, ?)",
        (summary, status, complexity, assignee, is_deferred, expires_at),
    )
    conn.commit()
    return cur.lastrowid


def _populate_backlog(db_file):
    """Seed a DB with one task per grooming category so every JSON key has signal."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        # Expired open task (not deferred — surfaces in `expired`, not autoclose).
        expired_id = _insert_task(
            conn,
            summary="Expired evaluation task",
            complexity="S",
            assignee="cli-engineer",
            expires_at="2000-01-01 00:00:00",
        )
        # Expired + deferred — autoclose should claim this one in non-dry-run.
        expired_deferred_id = _insert_task(
            conn,
            summary="Old deferred spike",
            complexity="S",
            assignee="cli-engineer",
            is_deferred=1,
            expires_at="2000-01-01 00:00:00",
        )
        # Unassigned + unsized — one row to exercise both categories.
        unassigned_unsized_id = _insert_task(
            conn, summary="Add a brand-new dashboard panel for WSJF scoring"
        )
        # A near-duplicate pair — the dupes heuristic needs two very similar
        # summaries among open tasks.
        dup_a = _insert_task(
            conn,
            summary="Groom the backlog by closing stale tickets",
            complexity="S",
            assignee="cli-engineer",
        )
        dup_b = _insert_task(
            conn,
            summary="Groom the backlog by closing stale tickets automatically",
            complexity="S",
            assignee="cli-engineer",
        )
        return {
            "expired": expired_id,
            "expired_deferred": expired_deferred_id,
            "unassigned_unsized": unassigned_unsized_id,
            "dup_a": dup_a,
            "dup_b": dup_b,
        }
    finally:
        conn.close()


class TestGroomDryRun:
    def test_json_shape_has_all_expected_keys(self, db_path):
        _populate_backlog(str(db_path))
        result = _run_groom(db_path, "--dry-run")
        assert result.returncode == 0, (
            f"tusk groom --dry-run failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        payload = json.loads(result.stdout)
        assert set(payload.keys()) == JSON_KEYS
        assert payload["dry_run"] is True
        assert set(payload["autoclose_candidates"].keys()) == AUTOCLOSE_KEYS
        assert payload["autoclose_candidates"]["applied"] is False
        assert isinstance(payload["lint"]["exit_code"], int)

    def test_expired_unassigned_unsized_surface_in_payload(self, db_path):
        ids = _populate_backlog(str(db_path))
        result = _run_groom(db_path, "--dry-run")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        expired_ids = {row["id"] for row in payload["expired"]}
        assert ids["expired"] in expired_ids
        assert ids["expired_deferred"] in expired_ids

        unassigned_ids = {row["id"] for row in payload["unassigned"]}
        assert ids["unassigned_unsized"] in unassigned_ids

        unsized_ids = {row["id"] for row in payload["unsized"]}
        assert ids["unassigned_unsized"] in unsized_ids

    def test_duplicates_detect_near_identical_summaries(self, db_path):
        ids = _populate_backlog(str(db_path))
        result = _run_groom(db_path, "--dry-run")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        pairs = {
            frozenset((pair["task_a"]["id"], pair["task_b"]["id"]))
            for pair in payload["duplicates"]
        }
        assert frozenset((ids["dup_a"], ids["dup_b"])) in pairs

    def test_dry_run_reports_autoclose_candidates_without_closing(self, db_path):
        ids = _populate_backlog(str(db_path))
        result = _run_groom(db_path, "--dry-run")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        candidates = payload["autoclose_candidates"]
        assert ids["expired_deferred"] in candidates["expired_deferred"]["task_ids"]
        assert candidates["expired_deferred"]["count"] >= 1
        assert candidates["applied"] is False

        # The DB must still show the candidate as open (dry-run is read-only).
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT status, closed_reason FROM tasks WHERE id = ?",
                (ids["expired_deferred"],),
            ).fetchone()
        finally:
            conn.close()
        assert row == ("To Do", None)


class TestGroomApply:
    def test_autoclose_runs_and_closes_expired_deferred_row(self, db_path):
        ids = _populate_backlog(str(db_path))
        result = _run_groom(db_path)  # no --dry-run
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["dry_run"] is False
        assert payload["autoclose_candidates"]["applied"] is True
        assert ids["expired_deferred"] in payload["autoclose_candidates"][
            "expired_deferred"
        ]["task_ids"]

        # Row is now Done/expired in the DB.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT status, closed_reason FROM tasks WHERE id = ?",
                (ids["expired_deferred"],),
            ).fetchone()
        finally:
            conn.close()
        assert row == ("Done", "expired")

    def test_same_keys_emitted_whether_or_not_dry_run(self, db_path):
        _populate_backlog(str(db_path))
        dry = json.loads(_run_groom(db_path, "--dry-run").stdout)
        full = json.loads(_run_groom(db_path).stdout)
        assert set(dry.keys()) == set(full.keys()) == JSON_KEYS


class TestGroomHelp:
    def test_help_flag_prints_usage_and_exits_zero(self, db_path):
        result = _run_groom(db_path, "--help")
        assert result.returncode == 0, result.stderr
        assert "Usage: tusk groom" in result.stdout
        # Every documented JSON key must appear in --help so operators can
        # grep the help text and know what to expect.
        for key in ("expired", "duplicates", "unassigned", "unsized",
                    "autoclose_candidates"):
            assert key in result.stdout, f"--help omits key {key!r}"
        assert "--dry-run" in result.stdout

    def test_unknown_flag_rejected(self, db_path):
        result = _run_groom(db_path, "--bogus")
        assert result.returncode != 0
        assert "Unknown flag" in result.stderr or "Unknown flags" in result.stderr

"""Integration tests for tusk task-reopen's validate_status_transition
snapshot/restore path (issue #831).

Mirrors the coverage shape used for tusk-task-unstart's TestRegenTriggersFailureRestore
(issue #824). When ``tusk regen-triggers`` fails in the ``finally`` block,
``task-reopen`` must restore the trigger DDL from the pre-DROP snapshot so the
DB is never left without the status-transition guard.
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_task_reopen = _load("tusk-task-reopen")


def _insert_task(conn: sqlite3.Connection, *, status: str = "Done") -> int:
    closed_reason = "'completed'" if status == "Done" else "NULL"
    closed_at = "datetime('now')" if status == "Done" else "NULL"
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity,"
        f" priority_score, closed_reason, closed_at)"
        f" VALUES ('test task', ?, 'feature', 'Medium', 'S', 50, {closed_reason},"
        f" {closed_at})",
        (status,),
    )
    conn.commit()
    return cur.lastrowid


def _call(db_path, config_path, *args):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_reopen.main(
            [str(db_path), str(config_path), *[str(a) for a in args]]
        )
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out and out.startswith("{") else None
    return rc, result, err_buf.getvalue()


class TestRegenTriggersFailureRestore:
    """Issue #831: when `tusk regen-triggers` fails in the finally block,
    task-reopen must restore validate_status_transition from the pre-DROP
    snapshot so the DB is never left without the status-transition guard.
    Mirrors the TASK-414 / issue #824 fix shipped in task-unstart."""

    @staticmethod
    def _fake_regen_failure(*args, **kwargs):
        """Drop-in replacement for subprocess.run that simulates a failing
        regen-triggers without invoking the real binary. The first positional
        arg is the argv list; only the `tusk regen-triggers` call is
        intercepted."""
        argv = args[0] if args else kwargs.get("args", [])
        if isinstance(argv, list) and len(argv) >= 2 and argv[-1] == "regen-triggers":
            return subprocess.CompletedProcess(
                args=argv,
                returncode=1,
                stdout="",
                stderr="Error: config validator rejected newer keys\n",
            )
        return subprocess.run(*args, **kwargs)

    def test_regen_failure_restores_trigger_from_snapshot(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            task_id = _insert_task(conn, status="Done")
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_task_reopen.subprocess, "run", self._fake_regen_failure
        )

        rc, result, err = _call(db_path, config_path, task_id, "--force")

        # The reopen itself still succeeds — the regen failure is a warning,
        # not a fatal error.
        assert rc == 0, f"expected 0, got {rc}; stderr={err}"
        assert result is not None
        assert result["task"]["status"] == "To Do"
        assert result["task"]["closed_reason"] is None

        # The status-transition guard must still be present in sqlite_master
        # even though regen-triggers failed.
        conn = sqlite3.connect(str(db_path))
        try:
            triggers = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name='validate_status_transition'"
            ).fetchall()
            assert len(triggers) == 1, (
                "validate_status_transition should be restored from the "
                "pre-DROP snapshot when regen-triggers fails"
            )
        finally:
            conn.close()

        # The regen-failure warning must still be surfaced (the underlying
        # config problem is real and the user needs to address it). The
        # "restored from snapshot" phrasing matches task-unstart so retro
        # analytics can pattern-match across both scripts.
        assert "regen-triggers failed" in err
        assert "restored from snapshot" in err

    def test_regen_success_leaves_trigger_in_place_no_snapshot_warning(
        self, db_path, config_path
    ):
        """Happy path: regen-triggers succeeds, so the snapshot path is never
        exercised and no snapshot-restore warning is emitted."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            task_id = _insert_task(conn, status="Done")
        finally:
            conn.close()

        rc, result, err = _call(db_path, config_path, task_id, "--force")

        assert rc == 0, f"expected 0, got {rc}; stderr={err}"
        assert result is not None
        assert result["task"]["status"] == "To Do"
        # No regen failure → no warning at all.
        assert "regen-triggers failed" not in err
        assert "restored from snapshot" not in err

        # And the trigger is present (regen-triggers ran successfully).
        conn = sqlite3.connect(str(db_path))
        try:
            triggers = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name='validate_status_transition'"
            ).fetchall()
            assert len(triggers) == 1
        finally:
            conn.close()

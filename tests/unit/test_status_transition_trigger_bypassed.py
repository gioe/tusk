"""Unit tests for tusk-db-lib.py's status_transition_trigger_bypassed helper
(issue #844 — extract the snapshot/restore choreography that landed twice
already in bin/tusk-task-reopen.py and bin/tusk-task-unstart.py)."""

import importlib.util
import os
import sqlite3
from unittest import mock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_db_lib():
    spec = importlib.util.spec_from_file_location(
        "tusk_db_lib",
        os.path.join(SCRIPT_DIR, "tusk-db-lib.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_db_lib = _load_db_lib()


@pytest.fixture()
def conn_with_trigger(tmp_path):
    """SQLite DB with the validate_status_transition trigger and a tasks
    table. Trigger refuses any status DML on tasks so we can verify the
    helper actually drops it during the yield."""
    db_path = tmp_path / "scratch.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO tasks VALUES (1, 'In Progress');

        CREATE TRIGGER validate_status_transition
        BEFORE UPDATE OF status ON tasks
        BEGIN
            SELECT RAISE(ABORT, 'status transition blocked by trigger');
        END;
        """
    )
    yield conn
    conn.close()


def _trigger_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='trigger' AND name='validate_status_transition'"
    ).fetchone()
    return row is not None


class TestStatusTransitionTriggerBypassed:

    def test_yield_block_can_perform_blocked_update(self, conn_with_trigger):
        """Inside the with-block, the trigger is dropped so the otherwise-
        forbidden status update succeeds. After exit, regen-triggers fires
        (mocked here)."""
        with mock.patch.object(
            tusk_db_lib.subprocess, "run",
            return_value=mock.MagicMock(returncode=0, stdout="", stderr=""),
        ):
            with tusk_db_lib.status_transition_trigger_bypassed(conn_with_trigger):
                conn_with_trigger.execute(
                    "UPDATE tasks SET status = 'To Do' WHERE id = 1"
                )

        row = conn_with_trigger.execute(
            "SELECT status FROM tasks WHERE id = 1"
        ).fetchone()
        assert row[0] == "To Do"

    def test_without_helper_trigger_blocks_same_update(self, conn_with_trigger):
        """Sanity guard: the fixture's trigger really does block the
        backwards status move, so the test above is meaningful."""
        with pytest.raises(sqlite3.IntegrityError):
            conn_with_trigger.execute(
                "UPDATE tasks SET status = 'To Do' WHERE id = 1"
            )

    def test_exception_in_body_rolls_back(self, conn_with_trigger):
        """An exception inside the with-block triggers ROLLBACK — the
        UPDATE must not survive."""
        with mock.patch.object(
            tusk_db_lib.subprocess, "run",
            return_value=mock.MagicMock(returncode=0, stdout="", stderr=""),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                with tusk_db_lib.status_transition_trigger_bypassed(conn_with_trigger):
                    conn_with_trigger.execute(
                        "UPDATE tasks SET status = 'To Do' WHERE id = 1"
                    )
                    raise RuntimeError("boom")

        row = conn_with_trigger.execute(
            "SELECT status FROM tasks WHERE id = 1"
        ).fetchone()
        assert row[0] == "In Progress", (
            "UPDATE must roll back when the body raises"
        )

    def test_regen_failure_restores_snapshot_and_warns(self, conn_with_trigger, capsys):
        """When `tusk regen-triggers` returns non-zero on exit, the helper
        replays the snapshotted DDL so the guard is restored, and emits a
        single stderr warning naming the failure."""
        with mock.patch.object(
            tusk_db_lib.subprocess, "run",
            return_value=mock.MagicMock(
                returncode=1, stdout="", stderr="config drift: unknown key"
            ),
        ):
            with tusk_db_lib.status_transition_trigger_bypassed(conn_with_trigger):
                conn_with_trigger.execute(
                    "UPDATE tasks SET status = 'To Do' WHERE id = 1"
                )

        assert _trigger_exists(conn_with_trigger), (
            "snapshot must be replayed when regen-triggers fails"
        )
        captured = capsys.readouterr()
        assert "tusk regen-triggers failed" in captured.err
        assert "restored from snapshot" in captured.err
        assert "config drift" in captured.err

    def test_regen_failure_with_missing_snapshot_emits_manual_warning(
        self, tmp_path, capsys
    ):
        """When the trigger was already absent BEFORE the helper ran
        (snapshot=None), a regen failure produces the alternate 'run
        regen-triggers manually' warning instead of the snapshot-restored
        warning."""
        db_path = tmp_path / "no-trigger.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO tasks VALUES (1, 'In Progress')")
        conn.commit()

        with mock.patch.object(
            tusk_db_lib.subprocess, "run",
            return_value=mock.MagicMock(
                returncode=1, stdout="", stderr="config drift"
            ),
        ):
            with tusk_db_lib.status_transition_trigger_bypassed(conn):
                conn.execute("UPDATE tasks SET status = 'To Do' WHERE id = 1")

        captured = capsys.readouterr()
        assert "tusk regen-triggers failed" in captured.err
        assert "Run 'tusk regen-triggers' manually" in captured.err
        assert "restored from snapshot" not in captured.err
        conn.close()

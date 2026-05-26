"""Shared database and config utilities for tusk scripts.

Provides get_connection(), load_config(), and validate_enum() so every
tusk-*.py script can import them from one place instead of duplicating
the logic.

Imported via tusk_loader (hyphenated filename requires it):

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader

    _db_lib = tusk_loader.load("tusk-db-lib")
    get_connection = _db_lib.get_connection
    load_config = _db_lib.load_config      # optional — only scripts that need it
    validate_enum = _db_lib.validate_enum  # optional — validates a value against config list
"""

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time


def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a SQLite connection with row_factory and foreign keys enabled."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_config(config_path: str) -> dict:
    """Load and return the tusk config JSON."""
    with open(config_path) as f:
        return json.load(f)


def validate_enum(value, valid_values: list, field_name: str) -> str | None:
    """Validate a value against a config list. Returns error message or None."""
    if not valid_values:
        return None  # empty list = no validation
    if value not in valid_values:
        joined = ", ".join(valid_values)
        return f"Invalid {field_name} '{value}'. Valid: {joined}"
    return None


def checkpoint_wal(db_path: str, max_retries: int = 3) -> None:
    """Checkpoint and truncate the WAL, retrying if busy readers block it.

    Uses TRUNCATE mode (vs FULL) so the WAL file is zeroed out on success,
    preventing stale WAL data from being rolled back during branch switches
    or file-move sequences. Silently skips if the DB file does not exist.
    """
    if not os.path.exists(db_path):
        return
    print("Checkpointing WAL...", file=sys.stderr)
    last_row = None
    for attempt in range(max_retries):
        try:
            conn = get_connection(db_path)
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"Warning: WAL checkpoint failed: {e} — continuing.", file=sys.stderr)
            return
        last_row = row
        if row is None or (row[0] == 0 and row[1] == row[2]):
            return  # all pages flushed and WAL truncated
        if attempt < max_retries - 1:
            time.sleep(0.2)
    print(
        f"Warning: WAL checkpoint partially blocked after {max_retries} attempts "
        f"(busy={last_row[0]}, log={last_row[1]}, checkpointed={last_row[2]}) — "
        "pages may still be at risk.",
        file=sys.stderr,
    )


@contextlib.contextmanager
def status_transition_trigger_bypassed(conn: sqlite3.Connection):
    """Run a block with the ``validate_status_transition`` trigger dropped.

    Snapshots the trigger DDL, opens a ``BEGIN IMMEDIATE`` transaction, drops
    the trigger, yields to the caller's UPDATEs, then COMMITs (or ROLLBACKs
    on exception). After the transaction finalises, runs ``tusk regen-triggers``
    to reinstall the trigger. If regen-triggers fails (typically when
    ``tusk/config.json`` carries newer keys the installed validator does not
    accept — issues #824 / #831), the snapshot is replayed and a single
    warning is emitted on stderr.

    Both ``bin/tusk-task-unstart.py`` and ``bin/tusk-task-reopen.py`` need
    this choreography. Without the helper the snapshot/restore boilerplate
    has to be re-implemented in every new caller, and instance feedback
    has caught two recurrences already (TASK-414, TASK-426); this helper
    is the third-recurrence fix (issue #844).

    Caller pattern::

        with status_transition_trigger_bypassed(conn):
            conn.execute("UPDATE tasks SET status = 'To Do' WHERE id = ?", (task_id,))
    """
    trigger_row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='trigger' AND name='validate_status_transition'"
    ).fetchone()
    trigger_ddl = trigger_row[0] if trigger_row else None

    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TRIGGER IF EXISTS validate_status_transition")
        try:
            yield
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        regen = subprocess.run(
            ["tusk", "regen-triggers"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if regen.returncode != 0:
            msg = regen.stderr.strip() or regen.stdout.strip() or "(no output)"
            restored = False
            restore_err = None
            if trigger_ddl:
                try:
                    conn.execute(trigger_ddl)
                    restored = True
                except sqlite3.Error as exc:
                    restore_err = str(exc)
            if restored:
                print(
                    f"Warning: tusk regen-triggers failed (exit {regen.returncode}): {msg}\n"
                    "Status-transition guard restored from snapshot; the "
                    "underlying config problem still needs to be fixed "
                    "(run 'tusk regen-triggers' after addressing it).",
                    file=sys.stderr,
                )
            else:
                extra = (
                    f"Snapshot restore also failed: {restore_err}\n"
                    if restore_err
                    else ""
                )
                print(
                    f"Warning: tusk regen-triggers failed (exit {regen.returncode}): {msg}\n"
                    f"{extra}"
                    "Run 'tusk regen-triggers' manually to restore the status-transition guard.",
                    file=sys.stderr,
                )

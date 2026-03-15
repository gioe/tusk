"""Shared database and config utilities for tusk scripts.

Provides get_connection() and load_config() so every tusk-*.py script
can import them from one place instead of duplicating the 4-liner.

Imported via importlib (hyphenated filename requires it):

    import importlib.util
    import os

    def _load_db_lib():
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk-db-lib.py")
        _s = importlib.util.spec_from_file_location("tusk_db_lib", _p)
        _m = importlib.util.module_from_spec(_s)
        _s.loader.exec_module(_m)
        return _m

    _db_lib = _load_db_lib()
    get_connection = _db_lib.get_connection
    load_config = _db_lib.load_config  # optional — only scripts that need it
"""

import json
import os
import sqlite3
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

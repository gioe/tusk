"""TASK-247: `tusk migrate` should regenerate validation triggers as its
final step.

The migrate-side companion to TASK-246's trigger-drift detection in
`tusk validate`: validate surfaces drift, migrate heals it. Without
auto-regen, long-lived DBs silently lag the validation floor whenever
`bin/tusk-config-tools.py` gains new trigger coverage — a manual
`tusk regen-triggers` was previously required to pick it up.

These tests pin the contract:
  - dropped trigger → migrate restores it
  - clean DB → migrate is idempotent (trigger set unchanged)
  - unexpected validate_* trigger → migrate prunes it (DROP all + recreate)
"""

import os
import sqlite3
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run_migrate(db_file):
    env = {**os.environ, "TUSK_DB": str(db_file), "TUSK_QUIET": "1"}
    return subprocess.run(
        [TUSK_BIN, "migrate"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _trigger_names(db_file):
    conn = sqlite3.connect(str(db_file))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND name LIKE 'validate_%'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _drop_trigger(db_file, name):
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.commit()
    finally:
        conn.close()


def test_migrate_regenerates_dropped_trigger(db_path):
    """The core contract: a manually-DROPped validate_* trigger must come
    back after `tusk migrate` runs, with no separate `tusk regen-triggers`
    invocation required."""
    before = _trigger_names(db_path)
    assert "validate_status_transition" in before, (
        "fresh DB should have validate_status_transition installed by cmd_init"
    )

    _drop_trigger(db_path, "validate_status_transition")

    after_drop = _trigger_names(db_path)
    assert "validate_status_transition" not in after_drop, (
        "precondition: DROP TRIGGER must remove it from sqlite_master"
    )

    result = _run_migrate(db_path)
    assert result.returncode == 0, (
        f"tusk migrate failed; stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    after_migrate = _trigger_names(db_path)
    assert "validate_status_transition" in after_migrate, (
        "migrate must regenerate the dropped trigger from config — this is "
        "the heal-on-migrate contract that pairs with validate's drift detection"
    )


def test_migrate_idempotent_when_no_drift(db_path):
    """Running migrate against an already-up-to-date, drift-free DB should
    succeed and leave the validate_* trigger set byte-identical. Idempotency
    is what makes auto-regen safe to run on every migrate."""
    before = _trigger_names(db_path)

    result = _run_migrate(db_path)
    assert result.returncode == 0, (
        f"tusk migrate failed; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Schema is up to date" in result.stdout, (
        "fresh DB should not advance schema version"
    )

    after = _trigger_names(db_path)
    assert before == after, (
        "migrate against a clean DB must not add or remove validate_* triggers"
    )


def test_migrate_prunes_unexpected_validate_trigger(db_path):
    """An unexpected `validate_*` trigger (one not produced by the current
    config) should be removed by migrate's regen step. This is the inverse
    drift case to a missing trigger: lingering validators from an older
    config get pruned because regen DROPs all validate_* triggers before
    recreating from config."""
    extra_sql = (
        "CREATE TRIGGER validate_phantom_insert "
        "BEFORE INSERT ON tasks FOR EACH ROW "
        "WHEN 0 BEGIN SELECT RAISE(ABORT, 'phantom'); END"
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(extra_sql)
        conn.commit()
    finally:
        conn.close()

    assert "validate_phantom_insert" in _trigger_names(db_path), (
        "precondition: phantom trigger must be installed before migrate"
    )

    result = _run_migrate(db_path)
    assert result.returncode == 0, (
        f"tusk migrate failed; stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    assert "validate_phantom_insert" not in _trigger_names(db_path), (
        "regen must drop validate_* triggers not produced by current config"
    )

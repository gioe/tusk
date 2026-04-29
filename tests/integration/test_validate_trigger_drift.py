"""TASK-246: `tusk validate` must surface validation-trigger drift between
the JSON config and the live SQLite triggers in `sqlite_master`.

The trigger generator (`compute_expected_triggers` in
`bin/tusk-config-tools.py`) is the single source of truth — derived from
`statuses`, `priorities`, `closed_reasons`, `task_types`, etc. When new
trigger coverage lands and `tusk regen-triggers` is not rerun on a
long-lived DB, the validation silently disappears. These tests exercise
the four observable states the new check has to handle:

  - clean DB → exit 0, "Validation triggers OK." reported
  - missing trigger (someone DROPped it) → exit 1, name surfaced
  - stale trigger (live SQL differs from config) → exit 1, name surfaced
  - unexpected trigger (extra validate_* row not in current config) → exit 1
  - regen-triggers heals everything → re-validate exits 0

The companion test_init_validate_exit_propagation.py already covers the
config-schema half of `tusk validate`; this file locks in the additive
trigger-drift half so a future refactor cannot silently remove it.
"""

import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run_validate(db_file):
    """Run `tusk validate` against the given DB and return CompletedProcess."""
    env = {**os.environ, "TUSK_DB": str(db_file), "TUSK_QUIET": "1"}
    return subprocess.run(
        [TUSK_BIN, "validate"],
        env=env,
        capture_output=True,
        text=True,
    )


def _drop_trigger(db_file, name):
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.commit()
    finally:
        conn.close()


def _replace_trigger(db_file, drop_name, create_sql):
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(f"DROP TRIGGER IF EXISTS {drop_name}")
        conn.execute(create_sql)
        conn.commit()
    finally:
        conn.close()


def test_validate_clean_db_passes_and_reports_triggers_ok(db_path):
    """Baseline: a freshly-init'd DB should report config valid AND
    validation triggers OK. This pins the additive contract — drift
    detection runs but reports success when nothing has drifted."""
    result = _run_validate(db_path)
    assert result.returncode == 0, (
        f"validate against fresh DB should exit 0, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "Config is valid" in result.stdout, (
        "existing config-schema check must still run after the additive trigger "
        "drift check is bolted on"
    )
    assert "Validation triggers OK" in result.stdout, (
        "drift check should surface a positive 'OK' line on success so users see "
        "the check ran rather than silently skipped"
    )


def test_validate_detects_missing_trigger(db_path):
    """A DROPped validation trigger represents the most common drift mode
    (someone manually edited the DB, or a migration removed it without
    regenerating). The violation must name the missing trigger."""
    _drop_trigger(db_path, "validate_status_transition")

    result = _run_validate(db_path)
    assert result.returncode == 1, (
        f"validate must exit 1 when a trigger is missing, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "Trigger drift detected" in result.stderr
    assert "validate_status_transition" in result.stderr, (
        "drift output must name the specific trigger that disappeared so the user "
        "knows what regen will rebuild"
    )
    assert "missing trigger" in result.stderr
    assert "tusk regen-triggers" in result.stderr, (
        "violation message must point the user at the exact remediation command"
    )


def test_validate_detects_stale_trigger_sql(db_path):
    """A trigger whose live SQL no longer matches the config-derived form
    is the silent-coverage-loss case the task description targets — the
    trigger row exists, so a name-only check would miss it. The check
    must compare the SQL bodies."""
    stale_sql = (
        "CREATE TRIGGER validate_priority_insert "
        "BEFORE INSERT ON tasks FOR EACH ROW "
        "WHEN NEW.priority IS NOT NULL AND NEW.priority NOT IN ('Stale', 'Old') "
        "BEGIN SELECT RAISE(ABORT, 'stale validation'); END"
    )
    _replace_trigger(db_path, "validate_priority_insert", stale_sql)

    result = _run_validate(db_path)
    assert result.returncode == 1
    assert "stale trigger" in result.stderr
    assert "validate_priority_insert" in result.stderr
    assert "tusk regen-triggers" in result.stderr


def test_validate_detects_unexpected_trigger(db_path):
    """A `validate_*` trigger present in the DB but no longer produced by
    the config (e.g. a config field was removed but the trigger lingered)
    is also drift — and is the only category that name-only comparison
    could catch but ours generalizes."""
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

    result = _run_validate(db_path)
    assert result.returncode == 1
    assert "unexpected trigger" in result.stderr
    assert "validate_phantom_insert" in result.stderr


def test_regen_triggers_heals_drift(db_path):
    """End-to-end loop: validate detects → regen-triggers fixes → validate
    is clean. Locks in that the comparison the validator does and the SQL
    the generator emits stay byte-equivalent (after whitespace normalisation)
    forever — if they ever diverge, this test breaks."""
    _drop_trigger(db_path, "validate_status_transition")

    pre = _run_validate(db_path)
    assert pre.returncode == 1, "precondition: drift should be detected first"

    env = {**os.environ, "TUSK_DB": str(db_path), "TUSK_QUIET": "1"}
    regen = subprocess.run(
        [TUSK_BIN, "regen-triggers"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert regen.returncode == 0, (
        f"regen-triggers should succeed; stdout={regen.stdout!r} stderr={regen.stderr!r}"
    )

    post = _run_validate(db_path)
    assert post.returncode == 0, (
        "validate should pass after regen-triggers heals drift; "
        f"stdout={post.stdout!r} stderr={post.stderr!r}"
    )
    assert "Validation triggers OK" in post.stdout


def test_validate_preserves_existing_config_check(db_path, tmp_path, monkeypatch):
    """Regression guard for criterion 1088: the trigger-drift check must
    be ADDITIVE. If config.json is invalid, the existing config-schema
    check must still run and fail with its original 'Config validation
    failed' diagnostic — we have not replaced or short-circuited it."""
    # Point the project at a damaged config alongside the valid DB.
    bad_config_dir = tmp_path / "tusk"
    bad_config_dir.mkdir(exist_ok=True)
    bad_config = bad_config_dir / "config.json"
    bad_config.write_text(
        '{"statuses":["To Do"],"priorities":["High"],'
        '"closed_reasons":["completed"],'
        '"dupes":{"unknown_subkey":1}}'
    )

    env = {
        **os.environ,
        "TUSK_DB": str(db_path),
        "TUSK_QUIET": "1",
    }
    result = subprocess.run(
        [TUSK_BIN, "validate"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode != 0, (
        "validate must still fail on config-schema errors; the additive trigger "
        "check should not have masked them"
    )
    combined = result.stdout + result.stderr
    assert "Config validation failed" in combined, (
        "the existing 'Config validation failed' diagnostic must still surface — "
        "trigger-drift detection is additive, not replacing"
    )

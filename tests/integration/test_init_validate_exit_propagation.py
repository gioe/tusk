"""Regression test for issue #613: `tusk init` must exit non-zero when
its embedded `validate_config` step rejects the project's config.json,
even on a *fresh* install (no pre-existing DB).

The reporter saw the failure mode against v783, where a damaged config
caused `tusk init` to print `Config validation failed: …` but still exit
with status 0. The fix relies on `bin/tusk`'s `set -euo pipefail` plus
`cmd_init` calling `validate_config` as a bare statement — any non-zero
exit from the validator must therefore terminate `cmd_init` and surface
to the caller.

The companion test_init_force_preflight_guard.py covers a related but
distinct scenario — `--force` on an existing DB with a damaged config
(issue #533). This file specifically locks in the *fresh-install* exit
path so that a future edit which removes `set -euo pipefail` from
`bin/tusk`, or wraps `validate_config` in a swallowing construct
(`|| true`, an `if … ; then … fi` guard, etc.), is caught immediately.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


@pytest.fixture()
def git_tmp(tmp_path):
    """A tmp_path with a bare git repo so find_repo_root resolves to it."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    return tmp_path


def _fresh_init(tmp_path, config_text):
    """Pre-seed tusk/config.json with `config_text`, then run a fresh
    `tusk init` (no --force, no pre-existing DB). Returns the
    CompletedProcess and the expected DB path."""
    config_dir = tmp_path / "tusk"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(config_text)

    db_file = config_dir / "tasks.db"
    env = {**os.environ, "TUSK_DB": str(db_file), "TUSK_QUIET": "1"}
    result = subprocess.run(
        [TUSK_BIN, "init", "--skip-gitignore"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
    )
    return result, db_file


# Mirrors the failing test from issue #613 verbatim — an unknown dupes
# subkey is a config validation error that `tusk-config-tools.py validate`
# returns 1 for. `bin/tusk`'s `set -euo pipefail` is what propagates that
# exit code out of `cmd_init`; if pipefail is removed, the bare
# `validate_config` call no longer aborts the script and `cmd_init`
# silently continues to schema creation, returning 0.
INVALID_DUPES_CONFIG = (
    '{"statuses":["To Do"],"priorities":["High"],'
    '"closed_reasons":["completed"],'
    '"dupes":{"unknown_key":1}}'
)


def test_fresh_init_exits_nonzero_when_dupes_has_unknown_key(git_tmp):
    """Issue #613 reproducer: an unknown dupes subkey must make `tusk init`
    exit non-zero. The reporter observed exit 0 against v783; the fix is
    `set -euo pipefail` + a bare `validate_config` call in cmd_init."""
    result, _ = _fresh_init(git_tmp, INVALID_DUPES_CONFIG)
    assert result.returncode != 0, (
        "tusk init should exit non-zero when validate_config rejects the project config; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_fresh_init_surfaces_config_validation_failed_message(git_tmp):
    """The validator's diagnostic must remain visible on stderr — the fix
    propagates the exit code without swallowing the error message."""
    result, _ = _fresh_init(git_tmp, INVALID_DUPES_CONFIG)
    combined = result.stdout + result.stderr
    assert "Config validation failed" in combined, (
        "expected validator's 'Config validation failed' diagnostic to be surfaced; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_fresh_init_does_not_create_db_when_validate_config_fails(git_tmp):
    """Failure-path correctness: when `validate_config` aborts the script,
    the SCHEMA sqlite3 block in cmd_init never runs, so no DB file is
    written. If `set -euo pipefail` were removed, cmd_init would proceed
    past validate_config and create the DB anyway — this assertion is the
    most direct downstream signal of the exit-propagation regression."""
    result, db_file = _fresh_init(git_tmp, INVALID_DUPES_CONFIG)
    assert result.returncode != 0
    assert not db_file.exists(), (
        f"tusk init created a DB at {db_file} despite config validation failing — "
        "validate_config's non-zero exit was not propagated"
    )

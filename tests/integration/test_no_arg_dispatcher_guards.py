"""Regression for issue #643: dispatcher arms that silently swallowed extra args.

TASK-287 fixed `cmd_shell` (issue #591). TASK-288 extends the same `$# > 0`
guard to the remaining 8 no-arg arms in `bin/tusk`'s dispatcher: `path`,
`validate`, `git-default-branch`, `version`, `version-bump`, `migrate`,
`update-gitignore`, `regen-triggers`. Before the fix, `tusk version 99999`
exited 0 and printed the version — silently ignoring the trailing arg.
After, each guarded arm exits non-zero with a clear stderr error.

This test covers the extra-arg guard on every newly-guarded arm, plus a
bare-invocation smoke test on the two purely read-only arms (`path` and
`version`) to confirm the guard does not regress the no-arg path.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

GUARDED_ARMS = [
    "path",
    "validate",
    "git-default-branch",
    "version",
    "version-bump",
    "migrate",
    "update-gitignore",
    "regen-triggers",
]


@pytest.mark.parametrize("arm", GUARDED_ARMS)
def test_extra_arg_exits_nonzero(arm, db_path):
    result = subprocess.run(
        [TUSK_BIN, arm, "bogus"],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    assert result.returncode != 0, (
        f"tusk {arm} with positional args must exit non-zero; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("arm", GUARDED_ARMS)
def test_extra_arg_error_names_command(arm, db_path):
    result = subprocess.run(
        [TUSK_BIN, arm, "bogus"],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    assert f"tusk {arm}" in result.stderr, (
        f"error message must name the offending command 'tusk {arm}'; "
        f"stderr={result.stderr!r}"
    )
    assert "takes no arguments" in result.stderr, (
        f"error message must say 'takes no arguments'; stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("arm", GUARDED_ARMS)
def test_extra_arg_error_to_stderr_not_stdout(arm, db_path):
    result = subprocess.run(
        [TUSK_BIN, arm, "bogus"],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    assert result.stdout == "", (
        f"usage error must go to stderr, not stdout; stdout={result.stdout!r}"
    )


@pytest.mark.parametrize("arm", ["path", "version"])
def test_bare_invocation_still_works(arm, db_path):
    """Smoke test on the two purely read-only arms — the guard must not
    regress the no-arg path. `path` and `version` are chosen because
    they're idempotent and produce deterministic output."""
    result = subprocess.run(
        [TUSK_BIN, arm],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    assert result.returncode == 0, (
        f"bare `tusk {arm}` must exit 0; stderr={result.stderr!r}"
    )
    assert result.stdout.strip() != "", (
        f"bare `tusk {arm}` must produce stdout output; stderr={result.stderr!r}"
    )

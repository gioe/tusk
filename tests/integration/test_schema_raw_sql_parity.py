"""Pin raw-SQL passthrough vs named-subcommand parity for the schema-version
preflight (issue #930).

The reporter on tusk v1012 observed `tusk "SELECT 1"` and `tusk -header -column
"SELECT ..."` refusing with the schema-mismatch error while named subcommands
(`skill-run`, `merge`, `task-summary`, `task-get`, `scope list`, `jots`,
`retro-themes`, `report-issue`) ran fine against the same v74 DB. Their concern
was that some surfaces silently operate on a too-new DB while others refuse —
breaking /retro Step LR-3 (raw-SQL) mid-flow while the rest of /retro
(subcommand-driven) completes.

Audit of bin/tusk and bin/tusk-resolve-schema-bin.py shows both surfaces share
the same `preflight_schema_version` entry, the same worktree-binary resolver,
and the same exec-or-bail terminus — there is no path divergence in the source
of v1024. This test pins that invariant: when the DB is stamped past
SUPPORTED_SCHEMA_MAX and no compatible worktree binary exists, both surfaces
must produce the IDENTICAL exit code and the IDENTICAL advisory wording. A
future preflight refactor that accidentally introduces asymmetry breaks this
test before it ships.

Scope note: the with-fallback parity (both surfaces redirect into the same
worktree binary via the resolver) is already covered by
``test_schema_dispatcher_fallback.py`` for the subcommand surface; that file's
TestDispatcherSchemaFallback class exercises the resolver-found path with
``task-list``. This file focuses on the no-fallback path because that is the
one where the reporter's symptom manifests (resolver returns empty, both
surfaces must hard-block consistently).
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate", os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


def _supported_schema_max():
    return max(v for v, _ in tusk_migrate.MIGRATIONS)


def _stamp_user_version(db, version):
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


def _run(args, db_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    return subprocess.run(
        [TUSK_BIN, *args], capture_output=True, text=True, env=env,
    )


# Subcommand surface anchor. `task-list` is read-only, is NOT in the preflight
# bypass list, and represents the broad class of Python-helper-dispatched
# subcommands whose ergonomics matter to /retro.
SUBCOMMAND_ARGV = ["task-list"]

# Raw-SQL surface variants. The reporter saw both the flag-prefixed and
# bare-SQL forms refuse; both must pin the same parity. The dispatcher routes
# anything that does not match a named case to ``cmd_query``, which is the
# raw-SQL passthrough.
RAW_SQL_VARIANTS = [
    pytest.param(["SELECT 1"], id="bare-sql"),
    pytest.param(["-column", "SELECT 1"], id="column-flag"),
    pytest.param(["-header", "-column", "SELECT 1"], id="header-and-column"),
]


@pytest.mark.parametrize("raw_sql_argv", RAW_SQL_VARIANTS)
def test_raw_sql_and_subcommand_block_identically_when_no_fallback(
    db_path, raw_sql_argv,
):
    """Without a compatible worktree binary the resolver returns empty and
    bin/tusk falls through to the legacy mismatch error. Both surfaces must
    return the SAME non-zero exit code and emit the SAME advisory line.

    Wording branches on source-repo vs consumer install. These integration
    tests run from inside the canonical tusk source repo, so the source-repo
    branch fires — the assertion checks the wording present on both surfaces,
    not the specific branch. The point is that whichever branch fires, BOTH
    surfaces fire the SAME one.
    """
    supported = _supported_schema_max()
    _stamp_user_version(db_path, supported + 1)

    sub_result = _run(SUBCOMMAND_ARGV, db_path)
    raw_result = _run(raw_sql_argv, db_path)

    # Both surfaces refused; same exit code.
    assert sub_result.returncode != 0, (
        f"Expected subcommand to refuse, got exit 0\nstderr: {sub_result.stderr!r}"
    )
    assert raw_result.returncode != 0, (
        f"Expected raw-SQL to refuse, got exit 0\nstderr: {raw_result.stderr!r}"
    )
    assert sub_result.returncode == raw_result.returncode, (
        f"Exit codes diverge: subcommand={sub_result.returncode}, "
        f"raw-SQL={raw_result.returncode}"
    )

    # Same canonical mismatch message on both. Both must name the same DB
    # version and the same supported_max — the user must not see one surface
    # cite a different version than the other.
    for r, label in [(sub_result, "subcommand"), (raw_result, "raw-SQL")]:
        assert "Schema mismatch" in r.stderr, (
            f"{label} missing 'Schema mismatch' advisory:\n{r.stderr!r}"
        )
        assert f"v{supported + 1}" in r.stderr, (
            f"{label} missing v{supported + 1} in advisory:\n{r.stderr!r}"
        )
        assert f"<=v{supported}" in r.stderr, (
            f"{label} missing <=v{supported} in advisory:\n{r.stderr!r}"
        )

    # Stronger parity check: extract the canonical advisory line from each
    # stderr and assert byte-for-byte equality. If a future change reshapes
    # one path's wording, this catches the divergence.
    def _advisory_line(stderr):
        for line in stderr.splitlines():
            if line.startswith("Schema mismatch:"):
                return line
        return None

    sub_line = _advisory_line(sub_result.stderr)
    raw_line = _advisory_line(raw_result.stderr)
    assert sub_line is not None
    assert raw_line is not None
    assert sub_line == raw_line, (
        f"Advisory text diverges between surfaces.\n"
        f"  subcommand: {sub_line!r}\n"
        f"  raw-SQL:    {raw_line!r}"
    )


def test_raw_sql_and_subcommand_both_pass_when_at_supported_max(db_path):
    """Happy path: user_version == supported_max means the preflight stays
    silent on both surfaces. The subcommand returns its own result and the
    raw-SQL passthrough returns sqlite3's result. Neither path sees the
    advisory."""
    _stamp_user_version(db_path, _supported_schema_max())

    sub_result = _run(SUBCOMMAND_ARGV, db_path)
    raw_result = _run(["SELECT 1"], db_path)

    assert sub_result.returncode == 0, (
        f"Subcommand unexpectedly failed at supported_max:\n{sub_result.stderr!r}"
    )
    assert raw_result.returncode == 0, (
        f"Raw-SQL unexpectedly failed at supported_max:\n{raw_result.stderr!r}"
    )
    # Neither surface fired the preflight advisory.
    assert "Schema mismatch" not in sub_result.stderr
    assert "Schema mismatch" not in raw_result.stderr
    # Raw-SQL passthrough printed sqlite3's actual result.
    assert raw_result.stdout.strip() == "1"

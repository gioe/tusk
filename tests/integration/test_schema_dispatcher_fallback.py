"""Regression test for issue #876.

TASK-457 (issue #866) added a schema-aware worktree-binary fallback to
``bin/tusk-merge.py``: when the primary's binary is at schema vN but the live
DB is at vN+1 (worktree-applied migration), ``_resolve_stable_tusk_bin``
falls back to the worktree's binary so the close-out subprocess calls don't
bail with the schema-mismatch error. That fix scoped only to the merge path.

This test exercises the generalization (issue #876): the bash dispatcher's
``preflight_schema_version`` itself now consults
``tusk-resolve-schema-bin.py`` and re-execs into a worktree-local binary that
supports the DB schema before emitting the existing "Schema mismatch"
diagnostic — covering every PATH-invoked subcommand, not just merge.

Setup mirrors the canonical scenario: primary's ``bin/tusk-migrate.py``
advertises [1..50]; a sibling worktree's ``bin/tusk-migrate.py`` advertises
[1..70]; the DB's ``PRAGMA user_version`` is 70. Invoking primary's bash
``bin/tusk`` with a non-bypass subcommand must exec into the worktree's
stub binary (verified via stdout sentinel) and surface the issue #876
stderr advisory.
"""

import os
import shutil
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE_TUSK = os.path.join(REPO_ROOT, "bin", "tusk")
SOURCE_RESOLVER = os.path.join(REPO_ROOT, "bin", "tusk-resolve-schema-bin.py")


def _write_migrate_py(path, max_version):
    """Write a minimal tusk-migrate.py whose MIGRATIONS registry advertises
    [1..max_version]. Mirrors the live registry shape that bin/tusk's
    preflight_schema_version greps."""
    lines = ["MIGRATIONS = [\n"]
    for v in range(1, max_version + 1):
        lines.append(f"    ({v}, migrate_{v}),\n")
    lines.append("]\n")
    path.write_text("".join(lines), encoding="utf-8")


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


@pytest.fixture()
def primary_and_worktree(tmp_path):
    """Build a real git repo with one worktree. Returns the primary-bin path,
    the worktree-bin path, and the DB path. The DB lives at
    ``<primary>/tusk/tasks.db`` so the helper's
    ``dirname(dirname(db_path))`` lands on the right repo root.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    _git(primary, "config", "user.email", "test@test")
    _git(primary, "config", "user.name", "test")

    primary_bin = primary / "bin"
    primary_bin.mkdir()
    shutil.copy(SOURCE_TUSK, primary_bin / "tusk")
    (primary_bin / "tusk").chmod(0o755)
    shutil.copy(SOURCE_RESOLVER, primary_bin / "tusk-resolve-schema-bin.py")
    (primary_bin / "tusk-resolve-schema-bin.py").chmod(0o755)
    _write_migrate_py(primary_bin / "tusk-migrate.py", max_version=50)

    # Make an initial commit so worktree-add works.
    (primary / "README").write_text("seed\n", encoding="utf-8")
    _git(primary, "add", "README")
    _git(primary, "commit", "-q", "-m", "seed")

    # Add a sibling worktree on a feature branch.
    wt = tmp_path / "wt"
    _git(primary, "worktree", "add", "-q", "-b", "feature/test", str(wt))
    wt_bin = wt / "bin"
    wt_bin.mkdir()
    # Stub binary: writes a sentinel naming its argv, exits 0. preflight's
    # exec replaces the primary process; the stub is what the operator
    # effectively ran.
    stub_path = wt_bin / "tusk"
    stub_path.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'WORKTREE_RAN: %s\\n' \"$*\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub_path.chmod(0o755)
    _write_migrate_py(wt_bin / "tusk-migrate.py", max_version=70)

    # DB at <primary>/tusk/tasks.db, stamped at user_version=70 (i.e. ahead of
    # primary's bin/tusk-migrate.py registry, which caps at 50).
    tusk_dir = primary / "tusk"
    tusk_dir.mkdir()
    db_path = tusk_dir / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 70")
        conn.commit()
    finally:
        conn.close()

    return {
        "primary": primary,
        "primary_bin": primary_bin / "tusk",
        "wt": wt,
        "wt_bin": wt_bin / "tusk",
        "db_path": db_path,
    }


def _run_primary(env_extras, *args, primary_bin):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # Silence cross-repo-drift warnings and the silent-exit guard
        # diagnostic noise that bin/tusk emits on captured-stderr paths.
        "TUSK_QUIET": "1",
        **env_extras,
    }
    return subprocess.run(
        [str(primary_bin), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


class TestDispatcherSchemaFallback:
    """End-to-end: primary's bash dispatcher must exec into the worktree's
    binary when the DB is ahead of primary's schema and the worktree's
    binary supports it."""

    def test_dispatcher_execs_worktree_binary_on_schema_mismatch(
        self, primary_and_worktree
    ):
        layout = primary_and_worktree
        result = _run_primary(
            {"TUSK_DB": str(layout["db_path"])},
            "task-list",
            primary_bin=layout["primary_bin"],
        )

        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        # Stub binary's sentinel proves the exec landed in the worktree.
        assert "WORKTREE_RAN: task-list" in result.stdout, (
            f"Expected worktree stub to receive 'task-list'; "
            f"stdout was {result.stdout!r}"
        )
        # Diagnostic advisory surfaces from the helper (criterion 2156).
        assert "issue #876" in result.stderr, (
            f"Expected stderr to name issue #876; got {result.stderr!r}"
        )
        # The legacy "Schema mismatch ... Run 'tusk upgrade'" line MUST NOT
        # fire when a fallback exists (criterion 2157, negative half).
        assert "Schema mismatch" not in result.stderr, (
            f"Legacy mismatch error fired despite valid fallback: "
            f"{result.stderr!r}"
        )

    def test_dispatcher_falls_through_when_no_fallback_exists(self, tmp_path):
        """Criterion 2157: when the helper finds no candidate, the existing
        'Schema mismatch ... Run tusk upgrade' diagnostic still fires."""
        primary = tmp_path / "primary"
        primary.mkdir()
        _git(primary, "init", "-q", "-b", "main")
        _git(primary, "config", "user.email", "test@test")
        _git(primary, "config", "user.name", "test")

        primary_bin = primary / "bin"
        primary_bin.mkdir()
        shutil.copy(SOURCE_TUSK, primary_bin / "tusk")
        (primary_bin / "tusk").chmod(0o755)
        shutil.copy(SOURCE_RESOLVER, primary_bin / "tusk-resolve-schema-bin.py")
        _write_migrate_py(primary_bin / "tusk-migrate.py", max_version=50)

        (primary / "README").write_text("seed\n", encoding="utf-8")
        _git(primary, "add", "README")
        _git(primary, "commit", "-q", "-m", "seed")

        # NO worktree added → helper has no candidates → bash falls through
        # to the legacy error path.
        tusk_dir = primary / "tusk"
        tusk_dir.mkdir()
        db_path = tusk_dir / "tasks.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA user_version = 70")
            conn.commit()
        finally:
            conn.close()

        result = _run_primary(
            {"TUSK_DB": str(db_path)},
            "task-list",
            primary_bin=primary_bin / "tusk",
        )

        assert result.returncode != 0
        assert "Schema mismatch" in result.stderr
        assert "v70" in result.stderr
        assert "<=v50" in result.stderr
        assert "tusk upgrade" in result.stderr
        # No exec happened → no worktree sentinel surfaces.
        assert "WORKTREE_RAN" not in result.stdout

    @pytest.mark.parametrize(
        "argv",
        [
            ["path"],
            ["version"],
            ["migrate"],
            ["regen-triggers"],
            ["resolve-schema-bin"],
        ],
    )
    def test_recovery_subcmds_bypass_fallback_path(
        self, primary_and_worktree, argv
    ):
        """Criterion 2158: bypass subcommands skip the preflight entirely —
        they do NOT trigger the fallback exec even when a compatible worktree
        binary exists. They run inside primary's own dispatch (or, for
        subcommands like ``path`` / ``version``, exit before any further
        processing). The stub sentinel must NOT appear in stdout."""
        layout = primary_and_worktree
        result = _run_primary(
            {"TUSK_DB": str(layout["db_path"])},
            *argv,
            primary_bin=layout["primary_bin"],
        )

        # We don't assert returncode==0 for every bypass subcommand because
        # `migrate` / `regen-triggers` / `resolve-schema-bin` may exit
        # non-zero against a minimal scaffold. The behavioral guarantee is
        # only that the fallback exec must not fire.
        assert "WORKTREE_RAN" not in result.stdout, (
            f"Bypass subcommand {argv!r} unexpectedly exec'd into worktree "
            f"stub; preflight should have skipped entirely.\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert "issue #876" not in result.stderr, (
            f"Bypass subcommand {argv!r} unexpectedly fired the issue #876 "
            f"diagnostic; preflight should have skipped entirely.\n"
            f"stderr: {result.stderr!r}"
        )

"""Regression test for issue #866.

The no-checkout fast-forward merge path pushes to origin/<default> without
advancing local <default>, so primary's bin/tusk-migrate.py can stay at vN
even after a worktree authored migration N+1. With the issue #841 fix,
_resolve_stable_tusk_bin prefers primary's bin/tusk over the worktree-local
fallback — but primary's bin/tusk's preflight then reads its own (stale)
sibling tusk-migrate.py and refuses with "Schema mismatch: this database is
at vN+1, but this tusk binary expects <=vN". session-close fails, the task
stays In Progress, and worktree cleanup never runs.

The fix is a schema-aware fallback inside _resolve_stable_tusk_bin: when the
chosen primary binary's tusk-migrate.py is behind the DB's PRAGMA
user_version AND the worktree-local fallback's tusk-migrate.py is current,
prefer the fallback. The worktree's binary is alive during the post-push
session-close / task-done calls — only post-merge cleanup risks removing
it, and that runs strictly after these subprocess calls (issue #846).
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.integration.conftest import _insert_session, _insert_task

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_merge = _load("tusk-merge")


def _write_migrate_py(path, max_version: int) -> None:
    """Write a minimal tusk-migrate.py whose MIGRATIONS registry advertises
    [1..max_version]. Mirrors the live registry shape that bin/tusk's
    preflight_schema_version greps, so _bin_supports_schema parses it the
    same way bash would.
    """
    lines = ["MIGRATIONS = [\n"]
    for v in range(1, max_version + 1):
        lines.append(f"    ({v},  migrate_{v}),\n")
    lines.append("]\n")
    path.write_text("".join(lines), encoding="utf-8")


class TestBinSupportsSchema:
    """Unit coverage of the helper that mirrors bin/tusk's preflight parsing."""

    def test_returns_true_when_max_version_meets_required(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "tusk").write_text("")
        _write_migrate_py(bin_dir / "tusk-migrate.py", max_version=70)

        assert tusk_merge._bin_supports_schema(str(bin_dir / "tusk"), 70) is True
        assert tusk_merge._bin_supports_schema(str(bin_dir / "tusk"), 50) is True

    def test_returns_false_when_max_version_below_required(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "tusk").write_text("")
        _write_migrate_py(bin_dir / "tusk-migrate.py", max_version=50)

        assert tusk_merge._bin_supports_schema(str(bin_dir / "tusk"), 70) is False

    def test_returns_true_when_no_migrate_py(self, tmp_path):
        """No sibling tusk-migrate.py → bash preflight returns 0 (no preflight
        to fail) → treat as compatible. Matches bin/tusk:1623-1624."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "tusk").write_text("")

        assert tusk_merge._bin_supports_schema(str(bin_dir / "tusk"), 70) is True

    def test_returns_true_when_registry_unparseable(self, tmp_path):
        """Migrate.py exists but has no parseable registry entries → bash
        preflight returns 0 (supported_max empty) → treat as compatible.
        Matches bin/tusk:1632."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "tusk").write_text("")
        (bin_dir / "tusk-migrate.py").write_text("# no registry yet\n", encoding="utf-8")

        assert tusk_merge._bin_supports_schema(str(bin_dir / "tusk"), 70) is True


class TestDbUserVersion:
    """Unit coverage of the PRAGMA user_version reader."""

    def test_reads_user_version(self, tmp_path):
        db = tmp_path / "tasks.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA user_version = 42")
        finally:
            conn.close()

        assert tusk_merge._db_user_version(str(db)) == 42

    def test_returns_zero_for_fresh_db(self, tmp_path):
        """PRAGMA user_version defaults to 0 on a fresh DB; the helper returns
        that as 0, not None. (None is reserved for sqlite errors.)"""
        db = tmp_path / "tasks.db"
        sqlite3.connect(str(db)).close()

        assert tusk_merge._db_user_version(str(db)) == 0

    def test_returns_none_when_db_missing(self, tmp_path):
        """sqlite3.connect on a path that doesn't exist creates a new DB, so
        a missing-file scenario actually returns 0. The error path is reserved
        for genuine sqlite errors (e.g. corruption); cover the happy fresh-DB
        case here and leave error injection to sqlite's own test surface."""
        # Pointing at a directory triggers sqlite3.OperationalError → None.
        result = tusk_merge._db_user_version(str(tmp_path))
        assert result is None


class TestResolveStableTuskBinSchemaAware:
    """Issue #866 — _resolve_stable_tusk_bin must fall back to the worktree-local
    binary when the primary's tusk-migrate.py is behind the DB schema and the
    worktree's tusk-migrate.py supports it.
    """

    @staticmethod
    def _make_layout(tmp_path, *, primary_max: int, fallback_max: int, db_version: int):
        """Build a source-repo style layout where:
          - primary's bin/tusk-migrate.py advertises [1..primary_max]
          - fallback (worktree) lives at <tmp_path>/.tusk-worktree/bin/tusk
            with a tusk-migrate.py advertising [1..fallback_max]
          - DB user_version is set to db_version
        Returns (db_path, primary_bin_path, fallback_bin_path).
        """
        # Primary repo layout
        primary_bin_dir = tmp_path / "bin"
        primary_bin_dir.mkdir()
        primary_bin = primary_bin_dir / "tusk"
        primary_bin.write_text("")
        _write_migrate_py(primary_bin_dir / "tusk-migrate.py", primary_max)

        # DB lives at <tmp_path>/tusk/tasks.db so dirname(dirname(db_path)) == tmp_path
        tusk_dir = tmp_path / "tusk"
        tusk_dir.mkdir()
        db_path = tusk_dir / "tasks.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA user_version = {db_version}")
        finally:
            conn.close()

        # Worktree-style fallback lives outside the primary repo
        fallback_bin_dir = tmp_path / ".tusk-worktree" / "bin"
        fallback_bin_dir.mkdir(parents=True)
        fallback_bin = fallback_bin_dir / "tusk"
        fallback_bin.write_text("")
        _write_migrate_py(fallback_bin_dir / "tusk-migrate.py", fallback_max)

        return str(db_path), str(primary_bin), str(fallback_bin)

    def test_falls_back_to_worktree_when_primary_schema_stale(self, tmp_path, capsys):
        """The canonical issue #866 scenario: primary is at vN, worktree authored
        migration N+1, DB is at vN+1. Returns the worktree's binary and prints a
        single-line diagnostic to stderr."""
        db_path, primary_bin, fallback_bin = self._make_layout(
            tmp_path, primary_max=69, fallback_max=70, db_version=70
        )

        result = tusk_merge._resolve_stable_tusk_bin(db_path, fallback_bin)

        assert result == fallback_bin
        err = capsys.readouterr().err
        assert "issue #866" in err
        assert primary_bin in err
        assert fallback_bin in err

    def test_uses_primary_when_both_support_schema(self, tmp_path, capsys):
        """The common case: primary and worktree are both current. Existing
        issue #834 protection (prefer primary so it survives worktree cleanup)
        is preserved."""
        db_path, primary_bin, fallback_bin = self._make_layout(
            tmp_path, primary_max=70, fallback_max=70, db_version=70
        )

        result = tusk_merge._resolve_stable_tusk_bin(db_path, fallback_bin)

        assert result == primary_bin
        assert "issue #866" not in capsys.readouterr().err

    def test_uses_primary_when_both_behind_schema(self, tmp_path, capsys):
        """Pathological case: DB is ahead of both binaries. Returns primary so
        the existing 'Schema mismatch ... Run tusk upgrade' diagnostic still
        surfaces — falling back to a fallback that ALSO can't service the schema
        would just shift which binary emits the same error."""
        db_path, primary_bin, fallback_bin = self._make_layout(
            tmp_path, primary_max=50, fallback_max=50, db_version=70
        )

        result = tusk_merge._resolve_stable_tusk_bin(db_path, fallback_bin)

        assert result == primary_bin
        assert "issue #866" not in capsys.readouterr().err

    def test_uses_primary_when_db_user_version_unreadable(self, tmp_path, capsys, monkeypatch):
        """sqlite error reading PRAGMA user_version → don't override the existing
        selection. Mirrors the behavior of bin/tusk's preflight, which returns 0
        when ``sqlite3 ... PRAGMA user_version`` exits non-zero (line 1623-1624).
        Forces None via monkeypatch rather than building a sqlite-failure DB path
        — the latter is platform-dependent (sqlite3.connect on a non-DB file may
        succeed lazily and only error on PRAGMA execution)."""
        db_path, primary_bin, fallback_bin = self._make_layout(
            tmp_path, primary_max=50, fallback_max=70, db_version=70
        )
        monkeypatch.setattr(tusk_merge, "_db_user_version", lambda _path: None)

        result = tusk_merge._resolve_stable_tusk_bin(db_path, fallback_bin)

        assert result == primary_bin
        assert "issue #866" not in capsys.readouterr().err

    def test_no_fallback_when_primary_equals_fallback(self, tmp_path, capsys):
        """Primary-checkout case: fallback IS the primary binary. The early
        return at the top of the function short-circuits before the schema
        check ever runs."""
        primary_bin_dir = tmp_path / "bin"
        primary_bin_dir.mkdir()
        primary_bin = primary_bin_dir / "tusk"
        primary_bin.write_text("")
        # Primary's migrate is stale, DB is ahead — but it doesn't matter
        # because the function short-circuits when primary == fallback.
        _write_migrate_py(primary_bin_dir / "tusk-migrate.py", 50)

        tusk_dir = tmp_path / "tusk"
        tusk_dir.mkdir()
        db_path = tusk_dir / "tasks.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA user_version = 70")
        finally:
            conn.close()

        result = tusk_merge._resolve_stable_tusk_bin(str(db_path), str(primary_bin))

        assert result == str(primary_bin)
        # No diagnostic — schema check was skipped, not failed.
        assert "issue #866" not in capsys.readouterr().err


class TestResolveStableTuskBinSchemaAwareClaudeBranch:
    """Issue #866 + non-source-repo (target project) layout — the schema check
    must also fire from the .claude/bin/tusk Claude branch of _resolve_stable_tusk_bin.
    Source-repo branch is covered by TestResolveStableTuskBinSchemaAware above.
    """

    def test_falls_back_to_worktree_from_claude_bin_branch(self, tmp_path, capsys):
        # Target project layout: .claude/bin/ but no bin/ + bin/tusk-migrate.py.
        claude_bin_dir = tmp_path / ".claude" / "bin"
        claude_bin_dir.mkdir(parents=True)
        claude_primary = claude_bin_dir / "tusk"
        claude_primary.write_text("")
        _write_migrate_py(claude_bin_dir / "tusk-migrate.py", max_version=69)

        tusk_dir = tmp_path / "tusk"
        tusk_dir.mkdir()
        db_path = tusk_dir / "tasks.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA user_version = 70")
        finally:
            conn.close()

        # Fallback is "ahead" — typical for the upgrade-in-flight case.
        fallback_bin_dir = tmp_path / ".tusk-worktree" / "bin"
        fallback_bin_dir.mkdir(parents=True)
        fallback_bin = fallback_bin_dir / "tusk"
        fallback_bin.write_text("")
        _write_migrate_py(fallback_bin_dir / "tusk-migrate.py", max_version=70)

        result = tusk_merge._resolve_stable_tusk_bin(str(db_path), str(fallback_bin))

        assert result == str(fallback_bin)
        assert "issue #866" in capsys.readouterr().err


@pytest.fixture()
def primary_install_repo_at_v50(tmp_path, config_path, monkeypatch):
    """Build a source-repo layout where primary's bin/tusk-migrate.py is
    pinned at v50 (intentionally behind the DB). The DB itself is initialized
    by the real bin/tusk init and then bumped to v70 to simulate "worktree
    just authored migration N+1, primary's .claude/bin/ has not been refreshed
    yet" — except in source-repo mode the primary candidate is bin/tusk, not
    .claude/bin/tusk.

    Layout::

        tmp_path/
            bin/
              tusk                 <- primary (stale) binary
              tusk-migrate.py      <- registry advertises [1..50]
            tusk/tasks.db          <- DB at user_version=70

    The worktree fallback lives outside tmp_path so the source-repo probe in
    _resolve_stable_tusk_bin finds primary first.
    """
    primary_bin_dir = tmp_path / "bin"
    primary_bin_dir.mkdir()
    primary_bin = primary_bin_dir / "tusk"
    primary_bin.write_text("#!/usr/bin/env bash\nexit 0\n")
    primary_bin.chmod(0o755)
    _write_migrate_py(primary_bin_dir / "tusk-migrate.py", max_version=50)

    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(parents=True, exist_ok=True)
    db_file = tusk_dir / "tasks.db"

    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [os.path.join(REPO_ROOT, "bin", "tusk"), "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    # Bump DB user_version to vN+1 (70) to simulate the worktree-just-added-N+1 state.
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("PRAGMA user_version = 70")
    finally:
        conn.close()

    return {"db_path": db_file, "primary_bin": primary_bin, "repo_root": tmp_path}


class TestNoCheckoutMergeUsesWorktreeBinaryOnSchemaMismatch:
    """End-to-end: when primary's bin/tusk is behind the DB schema, the no-checkout
    merge path must invoke session-close and task-done via the worktree-local
    fallback binary (which supports the schema).
    """

    def test_subprocess_calls_use_worktree_bin_when_primary_schema_stale(
        self, primary_install_repo_at_v50, config_path, tmp_path, monkeypatch
    ):
        db_path = primary_install_repo_at_v50["db_path"]
        primary_bin = primary_install_repo_at_v50["primary_bin"]

        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-schema-mismatch"

        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (branch, None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)

        # Build a worktree-local fallback whose tusk-migrate.py advertises v70
        # — i.e. the worktree just authored migration N+1.
        worktree_bin_dir = tmp_path / "worktree-bin"
        worktree_bin_dir.mkdir()
        worktree_bin = worktree_bin_dir / "tusk"
        worktree_bin.write_text("#!/usr/bin/env bash\nexit 0\n")
        worktree_bin.chmod(0o755)
        _write_migrate_py(worktree_bin_dir / "tusk-migrate.py", max_version=70)

        # main() passes os.path.join(script_dir, "tusk") as fallback where
        # script_dir = os.path.dirname(os.path.abspath(__file__)). Monkeypatch
        # __file__ so the fallback resolves to our synthesized worktree bin.
        monkeypatch.setattr(
            tusk_merge,
            "__file__",
            str(worktree_bin_dir / "tusk-merge.py"),
        )

        record: list[list[str]] = []

        def _mock_run(args, check=True):
            record.append(list(args))
            if args[:4] == ["git", "worktree", "list", "--porcelain"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=(
                        "worktree /tmp/repo-main\n"
                        "HEAD abc123\n"
                        "branch refs/heads/main\n"
                    ),
                    stderr="",
                )
            if args[:3] == ["git", "remote", "get-url"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="git@example.com:owner/repo.git\n", stderr=""
                )
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
            if args[:3] == ["git", "fetch", "origin"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "merge-base", "--is-ancestor"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "config", "--get"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            if args[:3] == ["git", "branch", "-D"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if "session-close" in args:
                return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
            if "task-done" in args:
                payload = json.dumps(
                    {
                        "task": {
                            "id": task_id,
                            "status": "Done",
                            "closed_reason": "completed",
                        },
                        "sessions_closed": 0,
                        "unblocked_tasks": [],
                    }
                )
                return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [
                    str(db_path),
                    str(config_path),
                    str(task_id),
                    "--session",
                    str(session_id),
                ]
            )

        assert rc == 0, (
            f"Expected exit 0\nstderr: {stderr_buf.getvalue()}\nrecord: {record}"
        )

        tusk_subcommands = [
            cmd for cmd in record if "session-close" in cmd or "task-done" in cmd
        ]
        assert tusk_subcommands, (
            f"Expected session-close and task-done subprocess calls; got: {record}"
        )

        # The fix: session-close and task-done must invoke the WORKTREE-local
        # binary (which supports the schema), not primary's stale bin/tusk.
        for cmd in tusk_subcommands:
            assert cmd[0] == str(worktree_bin), (
                f"Expected subprocess to invoke worktree-local binary "
                f"{worktree_bin} (issue #866 schema-aware fallback); "
                f"got {cmd[0]} in command {cmd}. primary={primary_bin}"
            )
            assert cmd[0] != str(primary_bin), (
                f"Subprocess invoked stale primary binary {primary_bin}; "
                "expected worktree-local fallback. This is the issue #866 regression."
            )

        # The diagnostic line must surface so operators can correlate the
        # behavior change with the pending migration.
        err = stderr_buf.getvalue()
        assert "issue #866" in err, (
            f"Expected stderr diagnostic naming issue #866; got:\n{err}"
        )

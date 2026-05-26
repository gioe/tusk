"""Unit tests for bin/tusk-resolve-schema-bin.py — gap-fills paths not
exercised by tests/integration/test_schema_dispatcher_fallback.py (issue #883).

Covers:
- _candidate_bins probe order including Codex (.claude/bin/tusk, tusk/bin/tusk)
- realpath self-loop guard via main()
- _db_user_version sqlite-error / null-row return-None branches
- _worktree_paths --porcelain parsing (multi-worktree, blank separators,
  non-zero exit, OSError)
- main() exits 1 when prereqs fail
"""

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_resolve_schema_bin",
    os.path.join(BIN, "tusk-resolve-schema-bin.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ---------------------------------------------------------------------
# _candidate_bins probe order
# ---------------------------------------------------------------------


class TestCandidateBins:
    def test_source_repo_layout_comes_first(self):
        assert mod._candidate_bins("/wt")[0] == os.path.join("/wt", "bin", "tusk")

    def test_claude_consumer_layout_comes_second(self):
        assert mod._candidate_bins("/wt")[1] == os.path.join(
            "/wt", ".claude", "bin", "tusk"
        )

    def test_codex_consumer_layout_comes_third(self):
        assert mod._candidate_bins("/wt")[2] == os.path.join(
            "/wt", "tusk", "bin", "tusk"
        )

    def test_returns_exactly_three_paths_in_canonical_order(self):
        assert mod._candidate_bins("/wt") == [
            "/wt/bin/tusk",
            "/wt/.claude/bin/tusk",
            "/wt/tusk/bin/tusk",
        ]


# ---------------------------------------------------------------------
# _db_user_version
# ---------------------------------------------------------------------


class TestDbUserVersion:
    def test_returns_int_for_valid_db(self, tmp_path):
        db = tmp_path / "tasks.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA user_version = 73")
        conn.close()
        assert mod._db_user_version(str(db)) == 73

    def test_returns_zero_for_fresh_db(self, tmp_path):
        db = tmp_path / "tasks.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.close()
        assert mod._db_user_version(str(db)) == 0

    def test_returns_none_for_non_sqlite_file(self, tmp_path):
        bogus = tmp_path / "not_a_db"
        bogus.write_bytes(b"not a sqlite database, just garbage")
        assert mod._db_user_version(str(bogus)) is None

    def test_returns_none_when_connect_raises(self, tmp_path, monkeypatch):
        def boom(*_a, **_kw):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(mod.sqlite3, "connect", boom)
        assert mod._db_user_version(str(tmp_path / "ignored.db")) is None

    def test_returns_none_when_pragma_yields_none_row(self, tmp_path, monkeypatch):
        class _Cursor:
            def fetchone(self):
                return None

        class _Conn:
            def execute(self, _sql):
                return _Cursor()

            def close(self):
                pass

        monkeypatch.setattr(mod.sqlite3, "connect", lambda *_a, **_kw: _Conn())
        assert mod._db_user_version(str(tmp_path / "ignored.db")) is None

    def test_returns_none_when_pragma_yields_null_value(self, tmp_path, monkeypatch):
        class _Cursor:
            def fetchone(self):
                return (None,)

        class _Conn:
            def execute(self, _sql):
                return _Cursor()

            def close(self):
                pass

        monkeypatch.setattr(mod.sqlite3, "connect", lambda *_a, **_kw: _Conn())
        assert mod._db_user_version(str(tmp_path / "ignored.db")) is None


# ---------------------------------------------------------------------
# _worktree_paths
# ---------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class TestWorktreePaths:
    def test_parses_single_worktree(self, monkeypatch):
        stdout = (
            "worktree /repo\n"
            "HEAD abcdef0123\n"
            "branch refs/heads/main\n"
        )
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *_a, **_kw: _FakeCompleted(0, stdout),
        )
        assert mod._worktree_paths("/repo") == ["/repo"]

    def test_parses_multiple_worktrees_with_blank_separators(self, monkeypatch):
        stdout = (
            "worktree /repo\n"
            "HEAD abcdef0123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo/.tusk/TASK-100\n"
            "HEAD 9988776655\n"
            "branch refs/heads/feature/TASK-100-foo\n"
            "\n"
            "worktree /repo/.tusk/TASK-200\n"
            "HEAD 1122334455\n"
            "detached\n"
        )
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *_a, **_kw: _FakeCompleted(0, stdout),
        )
        assert mod._worktree_paths("/repo") == [
            "/repo",
            "/repo/.tusk/TASK-100",
            "/repo/.tusk/TASK-200",
        ]

    def test_ignores_non_worktree_porcelain_lines(self, monkeypatch):
        stdout = (
            "worktree /repo\n"
            "HEAD aaaaaa\n"
            "branch refs/heads/main\n"
            "locked\n"
            "prunable some reason\n"
        )
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *_a, **_kw: _FakeCompleted(0, stdout),
        )
        assert mod._worktree_paths("/repo") == ["/repo"]

    def test_returns_empty_on_non_zero_exit(self, monkeypatch):
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *_a, **_kw: _FakeCompleted(128, ""),
        )
        assert mod._worktree_paths("/not_a_repo") == []

    def test_returns_empty_when_subprocess_raises_oserror(self, monkeypatch):
        def boom(*_a, **_kw):
            raise OSError("git binary missing")

        monkeypatch.setattr(mod.subprocess, "run", boom)
        assert mod._worktree_paths("/repo") == []

    def test_returns_empty_when_subprocess_raises_filenotfounderror(self, monkeypatch):
        def boom(*_a, **_kw):
            raise FileNotFoundError("git")

        monkeypatch.setattr(mod.subprocess, "run", boom)
        assert mod._worktree_paths("/repo") == []


# ---------------------------------------------------------------------
# main() — entrypoint integration
# ---------------------------------------------------------------------


def _make_db(path, version):
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()


def _make_bin(directory, max_migration_version):
    """Create a bin/tusk + bin/tusk-migrate.py pair inside `directory`.

    tusk-migrate.py advertises a MIGRATIONS registry up to
    `max_migration_version` so _bin_supports_schema parses it the same way
    bash would.
    """
    directory.mkdir(parents=True, exist_ok=True)
    tusk = directory / "tusk"
    tusk.write_text("#!/bin/sh\n")
    tusk.chmod(0o755)
    migrate = directory / "tusk-migrate.py"
    lines = ["MIGRATIONS = [\n"]
    for v in range(1, max_migration_version + 1):
        lines.append(f"    ({v},  migrate_{v}),\n")
    lines.append("]\n")
    migrate.write_text("".join(lines))
    return tusk


def _make_caller(repo_root):
    caller = repo_root / "bin" / "tusk"
    caller.parent.mkdir(parents=True, exist_ok=True)
    caller.write_text("#!/bin/sh\n")
    caller.chmod(0o755)
    return caller


def _setup_repo(tmp_path, db_version):
    """Return (repo, db, caller) for a standard tmp repo layout."""
    repo = tmp_path / "repo"
    (repo / "tusk").mkdir(parents=True)
    db = repo / "tusk" / "tasks.db"
    _make_db(db, db_version)
    caller = _make_caller(repo)
    return repo, db, caller


class TestMainEntrypoint:
    def test_exits_2_when_argv_count_wrong(self, capsys):
        assert mod.main(["tusk-resolve-schema-bin.py"]) == 2
        assert "usage" in capsys.readouterr().err.lower()

    def test_exits_1_when_db_user_version_returns_none(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "tusk").mkdir(parents=True)
        db = repo / "tusk" / "tasks.db"
        db.write_bytes(b"corrupt - not a sqlite db")
        caller = _make_caller(repo)
        assert (
            mod.main(["tusk-resolve-schema-bin.py", str(db), str(caller)]) == 1
        )

    def test_exits_1_when_no_worktrees(self, tmp_path, monkeypatch):
        _, db, caller = _setup_repo(tmp_path, 50)
        monkeypatch.setattr(mod, "_worktree_paths", lambda _start: [])
        assert (
            mod.main(["tusk-resolve-schema-bin.py", str(db), str(caller)]) == 1
        )

    def test_skips_candidate_that_loops_back_to_caller(
        self, tmp_path, monkeypatch, capsys
    ):
        # wt1's tusk is a symlink to the caller — realpath equality must
        # trigger the self-loop guard. wt2 is a clean candidate.
        _, db, caller = _setup_repo(tmp_path, 50)

        wt1 = tmp_path / "wt1"
        (wt1 / "bin").mkdir(parents=True)
        os.symlink(caller, wt1 / "bin" / "tusk")

        wt2 = tmp_path / "wt2"
        wt2_tusk = _make_bin(wt2 / "bin", 50)

        monkeypatch.setattr(
            mod, "_worktree_paths", lambda _start: [str(wt1), str(wt2)]
        )

        rc = mod.main(["tusk-resolve-schema-bin.py", str(db), str(caller)])
        assert rc == 0
        assert capsys.readouterr().out.strip() == str(wt2_tusk)

    def test_skips_candidate_whose_migrate_lacks_required_version(
        self, tmp_path, monkeypatch, capsys
    ):
        _, db, caller = _setup_repo(tmp_path, 70)

        wt1 = tmp_path / "wt1"
        _make_bin(wt1 / "bin", 50)  # too old, lacks v70
        wt2 = tmp_path / "wt2"
        wt2_tusk = _make_bin(wt2 / "bin", 80)  # advertises >= 70

        monkeypatch.setattr(
            mod, "_worktree_paths", lambda _start: [str(wt1), str(wt2)]
        )

        rc = mod.main(["tusk-resolve-schema-bin.py", str(db), str(caller)])
        assert rc == 0
        assert capsys.readouterr().out.strip() == str(wt2_tusk)

    def test_finds_codex_consumer_layout(self, tmp_path, monkeypatch, capsys):
        # Codex layout: only `tusk/bin/tusk` exists inside the worktree —
        # no `bin/tusk`, no `.claude/bin/tusk`.
        _, db, caller = _setup_repo(tmp_path, 60)

        codex_wt = tmp_path / "codex_wt"
        codex_tusk = _make_bin(codex_wt / "tusk" / "bin", 60)

        monkeypatch.setattr(mod, "_worktree_paths", lambda _start: [str(codex_wt)])

        rc = mod.main(["tusk-resolve-schema-bin.py", str(db), str(caller)])
        assert rc == 0
        assert capsys.readouterr().out.strip() == str(codex_tusk)

    def test_finds_claude_consumer_layout(self, tmp_path, monkeypatch, capsys):
        _, db, caller = _setup_repo(tmp_path, 60)

        claude_wt = tmp_path / "claude_wt"
        claude_tusk = _make_bin(claude_wt / ".claude" / "bin", 60)

        monkeypatch.setattr(mod, "_worktree_paths", lambda _start: [str(claude_wt)])

        rc = mod.main(["tusk-resolve-schema-bin.py", str(db), str(caller)])
        assert rc == 0
        assert capsys.readouterr().out.strip() == str(claude_tusk)

    def test_emits_diagnostic_to_stderr_on_match(
        self, tmp_path, monkeypatch, capsys
    ):
        _, db, caller = _setup_repo(tmp_path, 60)

        wt = tmp_path / "wt"
        _make_bin(wt / "bin", 60)
        monkeypatch.setattr(mod, "_worktree_paths", lambda _start: [str(wt)])

        mod.main(["tusk-resolve-schema-bin.py", str(db), str(caller)])
        err = capsys.readouterr().err
        assert "schema v60" in err
        assert "issue #876" in err

    def test_exits_1_when_all_candidates_skipped(
        self, tmp_path, monkeypatch
    ):
        _, db, caller = _setup_repo(tmp_path, 80)

        # Worktree exists but every bin within is too old.
        wt = tmp_path / "wt"
        _make_bin(wt / "bin", 50)
        monkeypatch.setattr(mod, "_worktree_paths", lambda _start: [str(wt)])

        assert (
            mod.main(["tusk-resolve-schema-bin.py", str(db), str(caller)]) == 1
        )

    def test_resolves_caller_path_when_file_missing(self, tmp_path, monkeypatch, capsys):
        # caller_bin path does not exist on disk — main() must fall back to
        # the raw string for the self-loop comparison rather than crashing.
        _, db, _caller = _setup_repo(tmp_path, 60)
        nonexistent_caller = tmp_path / "does_not_exist" / "tusk"

        wt = tmp_path / "wt"
        wt_tusk = _make_bin(wt / "bin", 60)
        monkeypatch.setattr(mod, "_worktree_paths", lambda _start: [str(wt)])

        rc = mod.main(
            ["tusk-resolve-schema-bin.py", str(db), str(nonexistent_caller)]
        )
        assert rc == 0
        assert capsys.readouterr().out.strip() == str(wt_tusk)

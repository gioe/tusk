"""Regression tests for the silent-exit safety net in bin/tusk.

The guard at the top of bin/tusk re-execs the dispatcher once with stderr
captured to a tempfile, then prints a generic diagnostic if the inner
invocation exited nonzero with empty stderr. These tests extract the guard
snippet from bin/tusk verbatim and exercise it against parameterized child
behavior, so any regression to the bash code is caught even when no real
tusk subcommand currently exits silently.

The integration is also smoke-tested against a known-good subcommand (`tusk
path`) to confirm the guard does not break normal flow.

See GitHub issue #785 / cluster:silent-failures.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BIN_TUSK = _REPO_ROOT / "bin" / "tusk"

_GUARD_START_MARKER = "# ── Silent-exit safety net"
_GUARD_END_AFTER = "# ── Resolve paths"


def _extract_guard_snippet() -> str:
    """Pull the safety-net block out of bin/tusk so tests stay in sync."""
    text = _BIN_TUSK.read_text()
    start = text.index(_GUARD_START_MARKER)
    # End is the blank line + next section header.
    end = text.index(_GUARD_END_AFTER, start)
    # Trim trailing blank line(s) before the next section.
    snippet = text[start:end].rstrip() + "\n"
    return snippet


def _make_harness(tmp_path: Path) -> Path:
    """Write a self-contained bash harness that wraps the guard snippet.

    The harness behaves like bin/tusk for the purposes of the guard: when the
    guard re-execs `"$0" "$@"`, the second invocation hits the `case` block at
    the bottom which mimics one of four child behaviors keyed by argv[1].

    The shebang must be at column 0 — do not use textwrap.dedent here, because
    the inserted snippet contains col-0 lines and the dedent's common-prefix
    detection then gives up, leaving the shebang indented and the file
    unexecutable.
    """
    snippet = _extract_guard_snippet()
    harness = tmp_path / "fake-tusk"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "\n"
        f"{snippet}\n"
        '# Inner: child behavior selected by first arg.\n'
        'case "${1:-}" in\n'
        "  silent5)\n"
        "    exit 5\n"
        "    ;;\n"
        "  loud5)\n"
        "    printf 'inner: loud failure\\n' >&2\n"
        "    exit 5\n"
        "    ;;\n"
        "  zero-silent)\n"
        "    exit 0\n"
        "    ;;\n"
        "  zero-loud)\n"
        "    printf 'inner: zero exit but writes to stderr\\n' >&2\n"
        "    exit 0\n"
        "    ;;\n"
        "  *)\n"
        '    printf \'inner: unknown test mode %s\\n\' "${1:-}" >&2\n'
        "    exit 2\n"
        "    ;;\n"
        "esac\n"
    )
    harness.chmod(harness.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return harness


def _run(harness: Path, arg: str, *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Strip any leaked guard state from the test runner's environment.
    env.pop("TUSK_GUARD_ACTIVE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(harness), arg],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    return _make_harness(tmp_path)


class TestSilentExitGuard:
    """End-to-end checks against the guard snippet extracted from bin/tusk."""

    def test_silent_nonzero_inner_gets_diagnostic(self, harness: Path):
        """The canonical issue-785 scenario: silent exit 5 → guard prints diagnostic."""
        result = _run(harness, "silent5")
        assert result.returncode == 5
        assert "exited 5 with no diagnostic output" in result.stderr
        assert "issues/785" in result.stderr
        assert result.stdout == ""

    def test_loud_nonzero_inner_no_double_print(self, harness: Path):
        """If inner already prints to stderr, guard must not add its diagnostic."""
        result = _run(harness, "loud5")
        assert result.returncode == 5
        assert "inner: loud failure" in result.stderr
        assert "exited 5 with no diagnostic output" not in result.stderr

    def test_zero_exit_no_diagnostic(self, harness: Path):
        """Successful runs must not trigger the guard regardless of stderr state."""
        result = _run(harness, "zero-silent")
        assert result.returncode == 0
        assert result.stderr == ""

    def test_zero_exit_with_stderr_passthrough(self, harness: Path):
        """Zero exits with stderr (warnings) must pass stderr through unchanged."""
        result = _run(harness, "zero-loud")
        assert result.returncode == 0
        assert "inner: zero exit but writes to stderr" in result.stderr
        assert "exited" not in result.stderr  # no diagnostic line

    def test_opt_out_disables_guard(self, harness: Path):
        """TUSK_SILENT_EXIT_GUARD=0 must skip the guard entirely (no diagnostic)."""
        result = _run(harness, "silent5", env_extra={"TUSK_SILENT_EXIT_GUARD": "0"})
        assert result.returncode == 5
        # Guard skipped → no diagnostic added.
        assert "no diagnostic output" not in result.stderr

    def test_guard_active_env_prevents_recursion(self, harness: Path):
        """When TUSK_GUARD_ACTIVE is already set, the guard must not re-engage.

        This mirrors the real-world case where `tusk skill-run finish` spawns
        `tusk call-breakdown` as a subprocess: the inner call inherits
        TUSK_GUARD_ACTIVE=1 and must skip the guard to avoid nested re-execs.
        """
        result = _run(harness, "silent5", env_extra={"TUSK_GUARD_ACTIVE": "1"})
        assert result.returncode == 5
        # Guard didn't engage → no diagnostic added even though inner was silent.
        assert "no diagnostic output" not in result.stderr


class TestSnippetExtraction:
    """Guard against the marker comments drifting (which would silently break the tests above)."""

    def test_snippet_markers_present_in_bin_tusk(self):
        text = _BIN_TUSK.read_text()
        assert _GUARD_START_MARKER in text, (
            "Silent-exit guard block start marker missing from bin/tusk — "
            "either the guard was removed (regression) or the marker comment "
            "was edited; update the marker constant in this test if intentional."
        )
        assert _GUARD_END_AFTER in text, (
            "Section delimiter after the guard ('# ── Resolve paths') missing — "
            "the snippet extraction in this test relies on it as the terminator."
        )

    def test_snippet_contains_diagnostic_string(self):
        snippet = _extract_guard_snippet()
        assert "no diagnostic output" in snippet
        assert "TUSK_SILENT_EXIT_GUARD" in snippet
        assert "TUSK_GUARD_ACTIVE" in snippet


class TestBinTuskSmoke:
    """Cross-check that adding the guard didn't break the real dispatcher."""

    def test_tusk_path_still_works(self):
        """`tusk path` is a read-only subcommand; must still print the DB path with exit 0."""
        env = os.environ.copy()
        env.pop("TUSK_GUARD_ACTIVE", None)
        result = subprocess.run(
            [str(_BIN_TUSK), "path"],
            capture_output=True,
            text=True,
            env=env,
            cwd=_REPO_ROOT,
        )
        assert result.returncode == 0, f"tusk path failed: stderr={result.stderr}"
        assert result.stdout.strip().endswith(".db")
        # The guard must NOT have fired on a successful invocation.
        assert "no diagnostic output" not in result.stderr


def _git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def _init_tusk_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(
        [
            "-c", "user.email=tusk@example.test",
            "-c", "user.name=Tusk Tests",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repo,
    )

    home = tmp_path / "home"
    home.mkdir()
    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{_REPO_ROOT / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    env.pop("TUSK_GUARD_ACTIVE", None)

    result = subprocess.run(
        [str(_BIN_TUSK), "init", "--force", "--skip-gitignore"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return repo, db_path, env


def _insert_skill_run(db_path: Path, skill_name: str = "tusk") -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at) VALUES (?, datetime('now', '-1 minute'))",
            (skill_name,),
        )
        conn.commit()
        return int(cur.lastrowid)


class TestCloseoutCommandsUnderGuard:
    """Issue #1024 regressions for real closeout commands under the guard."""

    def test_guarded_skill_run_finish_matches_guard_disabled_success(self, tmp_path: Path):
        repo, db_path, env = _init_tusk_repo(tmp_path)

        guarded_run_id = _insert_skill_run(db_path)
        guarded = subprocess.run(
            [str(_BIN_TUSK), "skill-run", "finish", str(guarded_run_id)],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert guarded.returncode == 0, guarded.stderr
        assert "Skill run" in guarded.stdout
        assert "Model:" in guarded.stdout
        assert "no diagnostic output" not in guarded.stderr

        unguarded_run_id = _insert_skill_run(db_path)
        no_guard_env = {**env, "TUSK_SILENT_EXIT_GUARD": "0"}
        unguarded = subprocess.run(
            [str(_BIN_TUSK), "skill-run", "finish", str(unguarded_run_id)],
            cwd=repo,
            env=no_guard_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert unguarded.returncode == 0, unguarded.stderr
        assert "Skill run" in unguarded.stdout
        assert "Model:" in unguarded.stdout
        assert "no diagnostic output" not in unguarded.stderr

    def test_guarded_skill_run_finish_missing_row_keeps_specific_error(self, tmp_path: Path):
        repo, _db_path, env = _init_tusk_repo(tmp_path)

        result = subprocess.run(
            [str(_BIN_TUSK), "skill-run", "finish", "99999"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        assert result.returncode == 1
        assert "No skill run found with id 99999" in result.stderr
        assert "no diagnostic output" not in result.stderr
